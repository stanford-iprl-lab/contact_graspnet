from visualization_utils import visualize_grasps, viz_proposals_mlab, viz_pts_and_eef_o3d
from contact_grasp_estimator import GraspEstimator
import config_utils
import os
import sys
import argparse
import numpy as np
import time
import glob
import open3d as o3d
from scipy.spatial.transform import Rotation
from scipy.spatial import KDTree

import tensorflow.compat.v1 as tf
tf.disable_eager_execution()
physical_devices = tf.config.experimental.list_physical_devices('GPU')
# Specify the GPU device to be used (e.g., GPU device 0)
tf.config.experimental.set_visible_devices(physical_devices[0], 'GPU')
tf.config.experimental.set_memory_growth(physical_devices[0], True)


class CGN_Grasp_Proposer:

    def __init__(self):

        # Load CGN network
        ckpt_dir = 'checkpoints/scene_test_2048_bs3_hor_sigma_001'
        global_config = config_utils.load_config(ckpt_dir, batch_size=1)

        global_config['TEST']['second_thres'] = 0.10
        global_config['DATA']['raw_num_points'] = 4096

        self.grasp_estimator = GraspEstimator(global_config)
        self.grasp_estimator.build_network()

        # Add ops to save and restore all the variables.
        saver = tf.train.Saver(save_relative_paths=True)

        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        self.sess = tf.Session(config=config)

        # Load weights
        self.grasp_estimator.load_weights(self.sess, saver, ckpt_dir, mode='test')

    def get_gripper_target_pos_from_cgn_grasp(self, cgn_grasp):
        """
        From CGN grasp representation, compute target position for mid-gripper point
        """
        # hardcoded from gripper control points
        # to be midpoint between fingertips
        P = np.array([[0.0,0.0,1.0527314e-01]])
        P = np.matmul(P, cgn_grasp[:3,:3].T)
        target_pos = P + np.expand_dims(cgn_grasp[:3, 3], 0)
        return np.squeeze(target_pos)

    def get_gripper_ori_from_cgn_grasp(self, cgn_grasp):
        R = Rotation.from_matrix(cgn_grasp[:3,:3]).as_euler("zxy", degrees=False)
        R[0] += np.pi/2
        R = Rotation.from_euler("zxy", R)
        R = R.as_matrix()
        ori_6d = np.concatenate([R[:,0], R[:,1]], axis=0)
        return ori_6d

    def propose_grasp_from_heatmap_file(
        self,
        heatmap_path,
        prop_save_dir=None,
        img_save_dir=None,
        heatmap_source="cgn",
        viz_o3d=False,
        viz_id=None,
        viz_top_k=None,
        viz_save_as_mp4=False,
        viz_all_grasps=True,
        viz_heatmap=False,
    ):

        # Read heatmap and pts from heatmap_file_path
        print(f"CGN on {heatmap_path}")
        heatmap_dict = np.load(heatmap_path, allow_pickle=True)["data"].item()
        pts = heatmap_dict["pts"]

        # Run inference
        # pred_grasps_cf is a dict with {-1: [list of grasps]}
        pred_grasps_cf, scores, contact_pts, gripper_openings = self.grasp_estimator.predict_scene_grasps(
            self.sess,
            pts,
            pc_segments={},
            local_regions=False,
            filter_grasps=False,
            forward_passes=1,
            pred_full=True
        )
        pred_grasps_cf = pred_grasps_cf[-1]
        gripper_openings = gripper_openings[-1]


        if heatmap_source == "gt":
            # Ground truth heatmap
            heatmap = heatmap_dict["gt_labels"]
        elif heatmap_source == "ours":
            # Predicted heatmap
            heatmap = heatmap_dict["labels"]
        elif heatmap_source == "cgn":
            # Use CGN scores
            heatmap = scores[-1]
        else:
            raise ValueError

        # Convert from contact point to gripper target point
        target_pos_list = []
        for cgn_grasp in pred_grasps_cf:
            target_pos = self.get_gripper_target_pos_from_cgn_grasp(cgn_grasp)
            target_pos_list.append(target_pos)
        target_pos_arr = np.array(target_pos_list)

        # Get index of top num_proposals heatmap scores
        num_proposals = 200 # TODO hardcoded. This is large because many points may match to same CGN point
        top_k = np.argsort(heatmap)[-num_proposals:]
        if heatmap_source == "cgn":
            # Using CGN pts (2048 points)
            top_k_pts = target_pos_arr[top_k]
            pts_to_viz = target_pos_arr
        elif heatmap_source in ["gt", "ours"]:
            # Using our input pcd
            top_k_pts = pts[top_k]
            pts_to_viz = pts
        else:
            raise ValueError

        # Find target_pos points from CGN that are closest to heatmap points
        kdtree = KDTree(target_pos_arr)
        closest = kdtree.query(top_k_pts)[1]

        # Filter predictions
        pred_grasps_cf = pred_grasps_cf[closest]
        closest_gripper_openings = gripper_openings[closest]
        scores[-1] = scores[-1][closest]
        closest_contact_pts = contact_pts[-1][closest]
        heatmap_closest = heatmap[top_k]
        pts_closest = pts[top_k]

        # get proposals
        k_target_posses = target_pos_arr[closest]
        #print("computed midpoint", k_target_posses[-1])
        proposals = []
        closest_ind_already_saved = []
        cgn_grasps_to_viz = []
        scores_to_viz = []
        for i, cgn_grasp in enumerate(pred_grasps_cf):
            # Don't save multiple proposals with same target_pos
            if closest[i] not in closest_ind_already_saved:
                closest_ind_already_saved.append(closest[i])
                ori_6d = self.get_gripper_ori_from_cgn_grasp(cgn_grasp)
                if heatmap_source == "cgn":
                    #cand = (closest_contact_pts[i], ori_6d, heatmap_closest[i], closest[i])
                    cand = (k_target_posses[i], ori_6d, heatmap_closest[i], closest[i])
                else:
                    cand = (k_target_posses[i], ori_6d, heatmap_closest[i], closest[i])

                    ## Adjust cgn grasp to have midpoint be our labeled point
                    #cand = (pts_closest[i], ori_6d, heatmap_closest[i], closest[i])
                    #diff = cgn_grasp[:3, 3] - k_target_posses[i]
                    #cgn_grasp[:3,3] = np.squeeze(pts_closest[i] + diff)

                proposals.append(cand)

                cgn_grasps_to_viz.append(cgn_grasp)
                scores_to_viz.append(heatmap_closest[i])
        
        print("Num proposals saved:", len(proposals))
        # Sort proposals from highest to lowest score
        proposals.sort(key=lambda data: -data[2])

        data_name = os.path.splitext(os.path.basename(heatmap_path))[0]

        if prop_save_dir is not None:
            # Save proposals in dict
            prop_dict = {
                "input": pts,
                "frame": "camera",
                "data_aug_mode": None,
                "gen_mode": "cgn",
                "gen_mode_param": f"heatmap_{heatmap_source}",
                "proposals": proposals,
                "cgn_grasps": cgn_grasps_to_viz,
                "cgn_grasps_scores": scores_to_viz,
                "pts_to_viz": pts_to_viz,
                "heatmap": heatmap,
            }
            prop_save_path = os.path.join(prop_save_dir, f"{data_name}.npz")
            np.savez_compressed(
                prop_save_path,
                data=prop_dict,
            )

        # Save proposal visualization
        if img_save_dir is not None:
            img_save_path = os.path.join(img_save_dir, f"{data_name}.png")

            # Load rgb from input_data/data_name.ply
            input_pcd_name = os.path.splitext(os.path.basename(img_save_path))[0] + ".ply"
            input_pcd_path = os.path.join(os.path.dirname(os.path.dirname(img_save_path)), "input_data", input_pcd_name)
            if not viz_heatmap and os.path.exists(input_pcd_path):
                in_pcd = o3d.io.read_point_cloud(input_pcd_path)
                if heatmap_source == "cgn":
                    pcd_rgb = np.array(in_pcd.colors) * 255.0
                    pts_to_viz = np.array(in_pcd.points)
                    bgcolor = (239/255., 196/255., 194/255.) # Light red
                else:
                    pcd_rgb = np.array(in_pcd.colors) * 255.0
                    bgcolor = (194/255., 214/255., 239/255.) # Light blue
            else:
                pcd_rgb = None
                bgcolor = (0.9, 0.9, 0.9) # Light gray
                
            viz_proposals_mlab(
                cgn_grasps_to_viz,
                scores_to_viz,
                pts_to_viz,
                heatmap,
                save_path=img_save_path,
                save_as_mp4=viz_save_as_mp4,
                draw_all_grasps=viz_all_grasps,
                highlight_id=viz_id,
                highlight_top_k=viz_top_k,
                pcd_rgb=pcd_rgb,
                bgcolor=bgcolor,
                #gripper_openings=closest_gripper_openings,
            )
        
        if viz_o3d:
            if pcd_rgb is not None: pcd_rgb /= 255.0
            # Open o3d visualizer
            self.viz_proposals_o3d(
                proposals,
                pts_to_viz,
                heatmap,
                save_path=None,
                pcd_rgb=pcd_rgb,
                highlight_top_k=viz_top_k,
                draw_all_grasps=viz_all_grasps,
            )


    def viz_proposals_o3d(
        self,
        proposals,
        pts,
        heatmap,
        save_path=None,
        highlight_top_k=None,
        pcd_rgb=None,
        draw_all_grasps=True,
    ):
        """
        Draw proposals on point cloud with heatmap labels as colors
        """

        target_pos_list = [prop[0] for prop in proposals]

        def get_ori_from_6d(r6d):
            def normalize(x):
                length = max(np.linalg.norm(x), 1e-8)
                return x / length

            r6d = r6d.reshape(2, 3)
            x, y = r6d
            x = normalize(x)
            y -= np.dot(x, y) * x
            y = normalize(y)
            z = np.cross(x, y, axis=-1)
            R = Rotation.from_matrix(np.stack([x, y, z], axis=-1))
            ori = R.as_euler("xyz")
            return ori

        target_ori_list = [
            get_ori_from_6d(prop[1])
            for prop in proposals
        ]

        if highlight_top_k is not None and not draw_all_grasps:
            target_ori_list = target_ori_list[:highlight_top_k]
            target_pos_list = target_pos_list[:highlight_top_k]

        viz_pts_and_eef_o3d(
            pts,
            target_pos_list,
            target_ori_list,
            frame="camera",
            heatmap_labels=heatmap,
            save_path=save_path,
            highlight_top_k=highlight_top_k,
            pcd_rgb=pcd_rgb,
        )