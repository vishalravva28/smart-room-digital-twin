"""
COLMAP point cloud -> clean, scaled, floor-aligned mesh for the digital twin.

1) Reconstruct with COLMAP (phone photos in ./photos):
     colmap automatic_reconstructor --workspace_path ws --image_path photos --dense 1
   The dense cloud is ws/dense/0/fused.ply

2) Run this script:
     pip install open3d numpy
     python scan_tools/process_scan.py ws/dense/0/fused.ply --room-width 6.2

   --room-width  real length (m) of the room's longest floor side, measured with a tape.
                 COLMAP has no real-world scale, so this sets it.
   --scale       alternative: multiply the model by this factor directly.

Output: static/models/room.glb  (meters, floor at y=0, centred on x/z)
Then set width/depth in engine.py LAYOUT to the size this script prints.
"""
import argparse
import os
import sys

import numpy as np
import open3d as o3d


def rotation_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if c < -0.999999:  # opposite vectors: rotate 180 deg about any perpendicular axis
        axis = np.cross(a, [1, 0, 0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0, 1, 0])
        axis /= np.linalg.norm(axis)
        return o3d.geometry.get_rotation_matrix_from_axis_angle(axis * np.pi)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cloud", help="COLMAP fused.ply (or any point cloud / mesh)")
    ap.add_argument("--out", default="static/models/room.glb")
    ap.add_argument("--room-width", type=float, help="real length in m of the longest floor side")
    ap.add_argument("--scale", type=float, help="manual scale factor (overrides --room-width)")
    ap.add_argument("--voxel", type=float, default=0.002, help="downsample voxel as a fraction of the largest model dimension")
    ap.add_argument("--depth", type=int, default=9, help="Poisson depth (8 = coarse, 10 = detailed)")
    ap.add_argument("--triangles", type=int, default=60000, help="target triangle count for the browser")
    a = ap.parse_args()

    print("Loading", a.cloud)
    pcd = o3d.io.read_point_cloud(a.cloud)
    if len(pcd.points) == 0:
        sys.exit("No points found in that file.")
    print("  points:", len(pcd.points))

    # 1. clean
    pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=30, std_ratio=2.0)
    ext = np.asarray(pcd.get_axis_aligned_bounding_box().get_extent())
    pcd = pcd.voxel_down_sample(a.voxel * ext.max())
    print("  after cleaning:", len(pcd.points))

    # 2. floor = largest plane; rotate its normal to +y
    plane, inliers = pcd.segment_plane(distance_threshold=0.01 * ext.max(), ransac_n=3, num_iterations=2000)
    n = np.array(plane[:3])
    R = rotation_between(n, np.array([0.0, 1.0, 0.0]))
    pcd.rotate(R, center=(0, 0, 0))
    pts = np.asarray(pcd.points)
    floor_y = np.median(pts[inliers, 1])
    if (pts[:, 1] > floor_y).mean() < 0.5:  # most points should sit above the floor
        pcd.rotate(o3d.geometry.get_rotation_matrix_from_xyz((np.pi, 0, 0)), center=(0, 0, 0))
        pts = np.asarray(pcd.points)
        floor_y = np.median(pts[inliers, 1])
    pcd.translate((0, -floor_y, 0))

    # 3. scale to meters
    pts = np.asarray(pcd.points)
    floor_pts = pts[np.abs(pts[:, 1]) < 0.03 * ext.max()]
    span = np.ptp(floor_pts[:, [0, 2]], axis=0) if len(floor_pts) > 10 else np.ptp(pts[:, [0, 2]], axis=0)
    if a.scale:
        s = a.scale
    elif a.room_width:
        s = a.room_width / span.max()
    else:
        s = 1.0
        print("WARNING: no --room-width or --scale given, model has COLMAP's arbitrary scale.")
    pcd.scale(s, center=(0, 0, 0))

    # 4. centre on x/z so the default model offset [0,0,0] lines up with the twin
    bb = pcd.get_axis_aligned_bounding_box()
    c = (bb.get_min_bound() + bb.get_max_bound()) / 2
    pcd.translate((-c[0], 0, -c[2]))
    bb = pcd.get_axis_aligned_bounding_box()
    size = bb.get_extent()
    print("Model size (m): x=%.2f  height=%.2f  z=%.2f" % (size[0], size[1], size[2]))

    # 5. mesh
    pcd.estimate_normals(o3d.geometry.KDTreeSearchParamKNN(30))
    pcd.orient_normals_consistent_tangent_plane(30)
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=a.depth)
    mesh.remove_vertices_by_mask(np.asarray(dens) < np.quantile(dens, 0.05))
    mesh = mesh.crop(bb)
    if len(mesh.triangles) > a.triangles:
        mesh = mesh.simplify_quadric_decimation(a.triangles)
    mesh.remove_degenerate_triangles()
    mesh.compute_vertex_normals()
    print("  triangles:", len(mesh.triangles))

    # 6. export
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    ok = o3d.io.write_triangle_mesh(a.out, mesh)
    if not ok:
        ply = os.path.splitext(a.out)[0] + ".ply"
        o3d.io.write_triangle_mesh(ply, mesh)
        sys.exit("This Open3D build cannot write .glb. Wrote %s - convert with: "
                 "python -c \"import trimesh; trimesh.load('%s').export('%s')\"" % (ply, ply, a.out))
    print("Saved", a.out)
    print("Next: set width=%.1f and depth=%.1f in engine.py LAYOUT, then refresh the twin." % (size[0], size[2]))


if __name__ == "__main__":
    main()
