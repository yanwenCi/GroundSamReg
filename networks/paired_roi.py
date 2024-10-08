import requests
import matplotlib.pyplot as plt
from transformers import SamModel, SamProcessor, pipeline
import random
import numpy as np
import cv2
import torch
from torch.nn.functional import cosine_similarity
import torch.nn.functional as F


class RoiMatching():
    def __init__(self,img1,img2, args=None, mode = 'embedding', url = "facebook/sam-vit-huge"):
        """
        Initialize
        :param img1: PIL image
        :param img2:
        """
        self.img1 = img1
        self.img2 = img2
        self.device = 'cuda'#args.device
        self.sam_type = 'sam_h'#args.sam_type
        self.url = url
        # everything mode
        self.mode = mode
        self.v_min = 20#args.v_min
        self.v_max = 1e6#args.v_max
        self.sim_criteria = 0.8#args.sim_criteria
        self.jacobian = True#args.jacobian_inverse

    def _sam_everything(self,imgs):
        generator = pipeline("mask-generation", model=self.url, device=self.device)
        if self.sam_type=='sam_h':
                outputs = generator(imgs, points_per_batch=64,pred_iou_thresh=0.90,stability_score_thresh=0.9,)
                # outputs = generator(imgs, points_per_batch=64,pred_iou_thresh=0.70,stability_score_thresh=0.7,)

        elif self.sam_type == 'medsam':
                # outputs = generator(imgs, points_per_batch=64,stability_score_thresh=0.7,) #medsam
                outputs = generator(imgs, points_per_batch=64,stability_score_thresh=0.8,) #medsam

        else:
                outputs = generator(imgs, points_per_batch=64, stability_score_thresh=0.9, )
        return outputs
    def _mask_criteria(self, masks, v_min=200, v_max= 70000, ovelap_ratio=0.9):
        remove_list = set()
        for _i, mask in enumerate(masks):
            if mask.sum() < v_min or mask.sum() > v_max:
                remove_list.add(_i)
        masks = [mask for idx, mask in enumerate(masks) if idx not in remove_list]
        n = len(masks)
        remove_list = set()
        for i in range(n):
            for j in range(i + 1, n):
                mask1, mask2 = masks[i], masks[j]
                intersection = (mask1 & mask2).sum()
                smaller_mask_area = min(masks[i].sum(), masks[j].sum())

                if smaller_mask_area > 0 and (intersection / smaller_mask_area) >= ovelap_ratio:
                    if mask1.sum() < mask2.sum():
                        remove_list.add(i)
                    else:
                        remove_list.add(j)
        return [mask for idx, mask in enumerate(masks) if idx not in remove_list]

    def _roi_proto(self, image_embeddings, masks):
        embs = []
        for _m in masks:
            # Convert mask to uint8, resize, and then back to boolean
            tmp_m = _m.astype(np.uint8)
            tmp_m = cv2.resize(tmp_m, (64, 64), interpolation=cv2.INTER_NEAREST)
            tmp_m = torch.tensor(tmp_m.astype(bool), device=self.device,
                                 dtype=torch.float32)  # Convert to tensor and send to CUDA
            tmp_m = tmp_m.unsqueeze(0).unsqueeze(0)  # Add batch and channel dimensions to match emb1

            # Element-wise multiplication with emb1
            tmp_emb = image_embeddings * tmp_m
            tmp_emb[tmp_emb == 0] = torch.nan
            emb = torch.nanmean(tmp_emb, dim=(2, 3))
            emb[torch.isnan(emb)] = 0
            embs.append(emb)
        return embs

    def _cosine_similarity(self, vec1, vec2):
        # Ensure vec1 and vec2 are 2D tensors [1, N]
        vec1 = vec1.view(1, -1)
        vec2 = vec2.view(1, -1)
        return cosine_similarity(vec1, vec2).item()

    def _similarity_matrix(self, protos1, protos2):
        # Initialize similarity_matrix as a torch tensor
        similarity_matrix = torch.zeros(len(protos1), len(protos2), device=self.device)
        for i, vec_a in enumerate(protos1):
            for j, vec_b in enumerate(protos2):
                similarity_matrix[i, j] = self._cosine_similarity(vec_a, vec_b)
                # print('RM: ', vec_a.max(), vec_b.max(),similarity_matrix[i,j])
        # Normalize the similarity matrix
        sim_matrix = (similarity_matrix - similarity_matrix.min()) / (similarity_matrix.max() - similarity_matrix.min())
        return similarity_matrix

    def _roi_match(self, matrix, masks1, masks2, sim_criteria=0.8):
        sim_criteria = self.sim_criteria
        # print(matrix.min(), matrix.max(),sim_criteria)
        index_pairs = []
        while torch.any(matrix > sim_criteria):
            max_idx = torch.argmax(matrix)
            max_sim_idx = (max_idx // matrix.shape[1], max_idx % matrix.shape[1])
            if matrix[max_sim_idx[0], max_sim_idx[1]] > sim_criteria:
                index_pairs.append(max_sim_idx)
            matrix[max_sim_idx[0], :] = -1
            matrix[:, max_sim_idx[1]] = -1
        masks1_new = []
        masks2_new = []
        for i, j in index_pairs:
            masks1_new.append(masks1[i])
            masks2_new.append(masks2[j])
        return masks1_new, masks2_new

    def _calculate_area_and_value(self, mask, image):
        """Helper function to calculate area and mean value."""
        area = mask.sum()
        mean_value = np.mean(np.expand_dims(mask, axis=-1) * image)
        return area, mean_value

    def _overlap_pair(self, masks1, masks2):
        self.masks1_cor = []
        self.masks2_cor = []
    
        for k, mask1 in enumerate(masks1[:-1], start=1):
            print(f'mask1 {k} is finding corresponding region mask...')
            a1, v1 = self._calculate_area_and_value(mask1, self.img1)
            overlap = mask1 * masks2[-1].astype(np.int64)
        
            if (overlap > 0).sum() / a1 > 0.1:
                counts = np.bincount(overlap.flatten())
                sorted_indices = np.argsort(counts)[::-1]
                top_two = sorted_indices[1:3]
            
                if len(top_two) < 2 or top_two[-1] == 0 or \
                    abs(counts[top_two[-1]] - counts[top_two[0]]) / max(counts[top_two[-1]], counts[top_two[0]]) < 0.2:
                    cor_ind = 0
                else:
                    a21, v21 = self._calculate_area_and_value(masks2[top_two[0]-1], self.img2)
                    a22, v22 = self._calculate_area_and_value(masks2[top_two[1]-1], self.img2)

                    # Compare areas and values
                    cor_ind = 0 if np.abs(a21 - a1) < np.abs(a22 - a1) else 1
                    if np.abs(v21 - v1) < np.abs(v22 - v1):
                        cor_ind = 0
                    else:
                        cor_ind = 1

                # Store corresponding masks
                self.masks1_cor.append(mask1)
                self.masks2_cor.append(masks2[top_two[cor_ind] - 1])
                

        # return masks1_new, masks2_new

    def get_paired_roi(self, mask1, mask2, emb1, emb2, mode='embedding'): 
        # batched_imgs = [self.img1, self.img2]
        # batched_outputs = self._sam_everything(batched_imgs)
        # self.masks1, self.masks2 = batched_outputs[0], batched_outputs[1] # 16.554s

        # self.masks1 = self._sam_everything(self.img1)  # len(RM.masks1) 2; RM.masks1[0] dict; RM.masks1[0]['masks'] list
        # self.masks2 = self._sam_everything(self.img2)
        self.masks1, self.masks2 = mask1, mask2
        # self.masks1 = self._mask_criteria(self.masks1, v_min=self.v_min, v_max=self.v_max)
        # self.masks2 = self._mask_criteria(self.masks2, v_min=self.v_min, v_max=self.v_max)

        if mode=='embedding':
            if len(self.masks1) > 0 and len(self.masks2) > 0:
                self.embs1 = self._roi_proto(emb1,self.masks1) #device:cuda1
                self.embs2 = self._roi_proto(emb2,self.masks2) # 6.752s
                self.sim_matrix = self._similarity_matrix(self.embs1, self.embs2)
                self.masks1_cor, self.masks2_cor = self._roi_match(self.sim_matrix,self.masks1,self.masks2,self.sim_criteria)
                if len(self.masks1_cor) > 0 and len(self.masks2_cor) > 0:
                    return torch.from_numpy(np.stack(self.masks1_cor)), torch.from_numpy(np.stack(self.masks2_cor))
                else:
                    return torch.tensor([]), torch.tensor([])
            else:
                return torch.tensor([]), torch.tensor([])
        elif mode=='overlaping':
            self._overlap_pair(self.masks1,self.masks2)
            return torch.from_numpy(np.stack(self.masks1_cor)), torch.from_numpy(np.stack(self.masks2_cor))
       
    def get_jacobian_matrix(self, simap, roimap, fc=False):
        simap.requires_grad_(True)
        _roimap = roimap.to(self.device)
        _roimap = _roimap.float()
        if fc:
            input_size = simap.shape[0] * simap.shape[1]
            output_size = roimap.shape[0] * roimap.shape[1]
            model = torch.nn.Linear(input_size, output_size)
            model.to(self.device)
            A_flat = simap.view(1, -1)
            predicted_B_flat = model(A_flat)
            predicted_B = predicted_B_flat.view(roimap.shape[0], roimap.shape[1])
            loss_fn = torch.nn.MSELoss()
            optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
            loss = loss_fn(predicted_B, _roimap)
            loss.backward()
            optimizer.step()

            jacobian = torch.autograd.functional.jacobian(lambda x: model(x.view(1, -1)).view(roimap.shape[0], roimap.shape[1]), simap)
            jacobian = jacobian.view(jacobian.shape[0]*jacobian.shape[1], jacobian.shape[2]*jacobian.shape[3])

        _roimap = _roimap.view(-1,1)
        _simap = simap.view(-1,1)
        R_T = _roimap.t()
        R_R_T = torch.mm(_roimap, R_T)
        # Final matrix J
        jacobian = torch.mm(torch.mm(_simap, R_T), torch.inverse(R_R_T))

        return jacobian

    def get_prompt_roi(self, prompt_1=torch.tensor([[10,10]]), rand_prompt=False, num_rand_prompt=1, multi_prompt_points=False):

        self.model = SamModel.from_pretrained(self.url).to(self.device)  # "facebook/sam-vit-huge" "wanglab/medsam-vit-base"
        self.processor = SamProcessor.from_pretrained(self.url)
        batched_imgs = [self.img1, self.img2]
        batched_outputs = self._get_image_embedding(batched_imgs)

        H,W = self.img1.size
        self.fix_rois, self.fix_protos = [],[]
        if rand_prompt:
            prompt_point = self._get_random_coordinates((H,W),num_rand_prompt)
        else:
            prompt_point = prompt_1

        self.emb1, self.emb2 = batched_outputs[0].unsqueeze(0), batched_outputs[1].unsqueeze(0)
        masks_f, scores_f = self._get_prompt_mask(self.img1, self.emb1, input_points=[prompt_point], labels=[1 for _ in range(prompt_point.shape[0])])
        # m[0].shape: torch.Size([1, 3, 834, 834]); tensor([[[0.9626, 0.9601, 0.7076]]], device='cuda:0')
        mask_f = masks_f[0][:,torch.argmax(scores_f[0][0]),:,:] # torch.Size([1, 834, 834])
        self.fix_rois.append(mask_f[0])
        self.n_coords = prompt_point
        if multi_prompt_points:
            n_coords = self._get_random_coordinates((H,W),2, mask=mask_f[0])
            self.n_coords = torch.cat((prompt_point,n_coords), dim=0)
            for _c in n_coords:
                masks_f, scores_f = self._get_prompt_mask(self.img1, self.emb1, input_points=[[_c]], labels=[1])
                self.fix_rois.append(masks_f[0][0,torch.argmax(scores_f[0][0]),:,:]) #torch.tensor(836,836)
            self.fix_rois = self._remove_duplicate_masks(self.fix_rois)
        for _m in self.fix_rois:
            self.fix_protos.append(self._get_proto(self.emb1,_m))

        self.mov_points = []
        self.mov_rois = []

        if self.jacobian:
            for idx,_p in enumerate(self.fix_protos):
                soft_fix_roi, fix_roi = self._generate_foreground_mask(_p,self.emb1,threshold=0.9)
                soft_fix_roi = soft_fix_roi.float()
                fix_roi = fix_roi.float()
                ori_fix_roi = self.fix_rois[idx]
                J = self.get_jacobian_matrix(fix_roi, ori_fix_roi)

                soft_mov_roi, mov_roi = self._generate_foreground_mask(_p, self.emb2, threshold=0.9)
                mov_roi = mov_roi.float()
                print(J.shape)
                print(mov_roi.view(-1,1).shape)
                mov_roi = torch.mm(J, mov_roi.view(-1,1))
                mov_roi = mov_roi.view(H,W)
                mov_roi = (mov_roi == mov_roi.max()).to(torch.bool)

                self.mov_points.append(mov_roi)
                proto_point = self._get_random_coordinates((H, W), 1, mask=mov_roi)
                proto_point = proto_point.detach().cpu().numpy()
                mov_rois, mov_scores = self._get_prompt_mask(self.img2, self.emb2, input_points=[proto_point],
                                                             labels=[1 for i in range(proto_point.shape[0])])
                mov_roi = mov_rois[0][0, torch.argmax(mov_scores[0][0]), :, :]
                self.mov_rois.append(mov_roi)
        else:
            for _p in self.fix_protos:
                soft_mov_roi, mov_roi = self._generate_foreground_mask(_p,self.emb2,threshold=0.9)
                mov_roi = mov_roi.float()
                mov_roi = F.interpolate(mov_roi.unsqueeze(0).unsqueeze(0), size=(H,W), mode='bilinear', align_corners=False)
                mov_roi = (mov_roi > 0).to(torch.bool)
                mov_roi = mov_roi.squeeze() # (200,200)
                self.mov_points.append(mov_roi)
                proto_point = self._get_random_coordinates((H,W),5, mask=mov_roi)
                proto_point = proto_point.detach().cpu().numpy()
                mov_rois, mov_scores = self._get_prompt_mask(self.img2, self.emb2, input_points=[proto_point], labels=[1 for i in range(proto_point.shape[0])])
                mov_roi = mov_rois[0][0,torch.argmax(mov_scores[0][0]),:,:]
                self.mov_rois.append(mov_roi)

        return self.fix_rois, self.mov_rois

    def _remove_duplicate_masks(self,masks):
        grouped_masks = {}

        for mask in masks:
            num_true_pixels = torch.sum(mask).item()
            # print(num_true_pixels)
            if num_true_pixels in grouped_masks:
                continue
            grouped_masks[num_true_pixels] = mask

        unique_masks = list(grouped_masks.values())
        return unique_masks

    def _get_proto(self,_emb,_m):
        tmp_m = torch.tensor(_m, device=self.device, dtype=torch.uint8)
        tmp_m = F.interpolate(tmp_m.unsqueeze(0).unsqueeze(0), size=(64, 64), mode='nearest').squeeze()
        # tmp_m = torch.tensor(tmp_m, device=self.device,
        #                      dtype=torch.float32)  # Convert to tensor and send to CUDA
        tmp_m = tmp_m.unsqueeze(0).unsqueeze(0)  # Add batch and channel dimensions to match emb1

        # Element-wise multiplication with emb1
        tmp_emb = _emb * tmp_m
        # (1,256,64,64)

        tmp_emb[tmp_emb == 0] = torch.nan
        emb = torch.nanmean(tmp_emb, dim=(2, 3))
        emb[torch.isnan(emb)] = 0
        return emb

    def _get_random_coordinates(self, shape, n_points, mask=None):
        """
        Generate random coordinates within a given shape. If a mask is provided,
        the points are generated within the non-zero regions of the mask.

        Parameters:
        - shape: tuple of (H, W), the dimensions of the space where points are generated.
        - n_points: int, the number of points to generate.
        - mask: torch tensor or None, the mask within which to generate points, or None to ignore.

        Returns:
        - coordinates: torch tensor of shape (n_points, 2), each row is a coordinate (y, x).
        """
        H, W = shape
        if mask is None:
            coordinates = torch.stack([torch.randint(0, W, (n_points,)),
                                       torch.randint(0, H, (n_points,))], dim=1)
        else:
            nonzero_indices = torch.nonzero(mask)

            if len(nonzero_indices) < n_points:
                raise ValueError("No enough points.")

            chosen_indices = torch.randperm(nonzero_indices.size(0))[:n_points]
            coordinates = nonzero_indices[chosen_indices]
            coordinates = coordinates[:, [1, 0]]


        return coordinates

    def _get_image_embedding(self, image):
        inputs = self.processor(image, return_tensors="pt").to(self.device)
        # # pixel_values" torch.size(1,3,1024,1024); "original_size" tensor([[834,834]]); 'reshaped_input_sizes' tensor([[1024, 1024]])
        image_embeddings = self.model.get_image_embeddings(inputs["pixel_values"])
        return image_embeddings

    def _get_prompt_mask(self, image, image_embeddings, input_points, labels, input_boxes=None):
        if input_boxes is not None:
            inputs = self.processor(image, input_boxes=[input_boxes], input_points=[input_points], input_labels=[labels],
                               return_tensors="pt").to(self.device)
        else:
            inputs = self.processor(image, input_points=[input_points], input_labels=[labels],
                                    return_tensors="pt").to(self.device)

        inputs.pop("pixel_values", None)
        inputs.update({"image_embeddings": image_embeddings})
        with torch.no_grad():
            outputs = self.model(**inputs)

        masks = self.processor.image_processor.post_process_masks(outputs.pred_masks.cpu(), inputs["original_sizes"].cpu(),
                                                             inputs["reshaped_input_sizes"].cpu())
        scores = outputs.iou_scores
        return masks, scores

    def _generate_foreground_mask(self, prototype, image_embedding, threshold=0.5):
        """
        Generate foreground mask based on cosine similarity between prototype and image embedding.

        Parameters:
        - prototype: torch tensor of shape [1, 256]
        - image_embedding: torch tensor of shape [1, 256, 64, 64]
        - threshold: threshold value for foreground mask

        Returns:
        - mask: torch tensor of shape [64, 64] representing the foreground mask
        """
        # Transpose prototype to match the dimensions of image_embedding_flat
        prototype = prototype.transpose(0, 1)  # Shape: [256, 1]

        # Flatten the image embedding
        image_embedding_flat = image_embedding.view(256, -1)  # Shape: [256, 64*64]

        # Expand prototype to match the size of image_embedding_flat
        prototype_expanded = prototype.expand(-1, image_embedding_flat.size(1))  # Shape: [256, 64*64]

        # Compute cosine similarity between prototype and image embedding
        similarity_map = F.cosine_similarity(prototype_expanded, image_embedding_flat, dim=0)

        # Reshape similarity to match the spatial dimensions of the image
        soft_mask = similarity_map.view(image_embedding.size(2), image_embedding.size(3))

        # Generate foreground mask based on threshold
        hard_mask = torch.where(soft_mask >= similarity_map.max(), torch.ones_like(soft_mask), torch.zeros_like(soft_mask))

        return soft_mask,hard_mask


def visualize_masks(image1, masks1, image2, masks2):
    # Convert PIL images to numpy arrays
    background1 = np.array(image1)
    background2 = np.array(image2)

    # Convert RGB to BGR (OpenCV uses BGR color format)
    background1 = cv2.cvtColor(background1, cv2.COLOR_RGB2BGR)
    background2 = cv2.cvtColor(background2, cv2.COLOR_RGB2BGR)

    # Create a blank mask for each image
    mask1 = np.zeros_like(background1)
    mask2 = np.zeros_like(background2)

    distinct_colors = [
        (255, 0, 0),  # Red
        (0, 255, 0),  # Green
        (0, 0, 255),  # Blue
        (255, 255, 0),  # Cyan
        (255, 0, 255),  # Magenta
        (0, 255, 255),  # Yellow
        (128, 0, 0),  # Maroon
        (0, 128, 0),  # Olive
        (0, 0, 128),  # Navy
        (128, 128, 0),  # Teal
        (128, 0, 128),  # Purple
        (0, 128, 128),  # Gray
        (192, 192, 192)  # Silver
    ]

    def random_color():
        """Generate a random color with high saturation and value in HSV color space."""
        hue = random.randint(0, 179)  # Random hue value between 0 and 179 (HSV uses 0-179 range)
        saturation = random.randint(200, 255)  # High saturation value between 200 and 255
        value = random.randint(200, 255)  # High value (brightness) between 200 and 255
        color = np.array([[[hue, saturation, value]]], dtype=np.uint8)
        return cv2.cvtColor(color, cv2.COLOR_HSV2BGR)[0][0]


    # Iterate through mask lists and overlay on the blank masks with different colors
    for idx, (mask1_item, mask2_item) in enumerate(zip(masks1, masks2)):
        # color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        # color = distinct_colors[idx % len(distinct_colors)]
        color = random_color()
        # Convert binary masks to uint8
        mask1_item = np.uint8(mask1_item)
        mask2_item = np.uint8(mask2_item)

        # Create a mask where binary mask is True
        fg_mask1 = np.where(mask1_item, 255, 0).astype(np.uint8)
        fg_mask2 = np.where(mask2_item, 255, 0).astype(np.uint8)

        # Apply the foreground masks on the corresponding masks with the same color
        mask1[fg_mask1 > 0] = color
        mask2[fg_mask2 > 0] = color

    # Add the masks on top of the background images
    result1 = cv2.addWeighted(background1, 1, mask1, 0.5, 0)
    result2 = cv2.addWeighted(background2, 1, mask2, 0.5, 0)

    return result1, result2

def visualize_masks_with_scores(image, masks, scores, points):
    """
    Visualize masks with their scores on the original image.

    Parameters:
    - image: PIL image with size (H, W)
    - masks: torch tensor of shape [1, 3, H, W]
    - scores: torch tensor of scores with shape [1, 3]
    """
    # Convert PIL image to NumPy array
    image_np = np.array(image)

    # Move masks and scores to CPU and convert to NumPy
    masks_np = masks.cpu().numpy().squeeze(0)  # Shape [3, H, W]
    scores_np = scores.cpu().numpy().squeeze(0)  # Shape [3]

    # Set up the plot
    fig, axs = plt.subplots(1, 3, figsize=(15, 5))
    for i in range(3):
        ax = axs[i]
        score = scores_np[i]
        mask = masks_np[i]
        # Create an RGBA image for the mask
        mask_image = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
        mask_image[mask] = [255, 0, 0, 255]
        # mask_image[..., 3] = mask * 255  # Alpha channel
        # Overlay the mask on the image
        ax.imshow(image_np)
        # ax.imshow(mask_image, cmap='Reds', alpha=0.5)
        ax.imshow(mask_image, alpha=0.5)
        ax.scatter(points[:, 0], points[:, 1], c='red', marker='o', label='Scatter Points')
        ax.set_title(f'Score: {score:.4f}')
        ax.axis('off')
    plt.tight_layout()
    # plt.show()

def visualize_masks_with_sim(image, masks):
    """
    Visualize masks with their scores on the original image.

    Parameters:
    - image: PIL image with size (H, W)
    - masks: torch tensor of shape [1, 3, H, W]
    - scores: torch tensor of scores with shape [1, 3]
    """
    # Convert PIL image to NumPy array
    image_np = np.array(image)

    # Move masks and scores to CPU and convert to NumPy

    masks = [m.cpu().numpy() for m in masks]  # Shape [3, H, W]
    masks = [m.astype('uint8') for m in masks]
    masks_np = np.array(masks)

    # Set up the plot
    fig, axs = plt.subplots(1, masks_np.shape[0], figsize=(15, 5))
    for i in range(masks_np.shape[0]):
        ax = axs[i]
        mask = masks_np[i]
        # Create an RGBA image for the mask
        mask_image = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
        mask_image[mask>0] = [255, 0, 0, 255]

        # mask_image[..., 3] = mask * 255  # Alpha channel
        # Overlay the mask on the image
        ax.imshow(image_np)
        ax.imshow(mask_image, cmap='Reds', alpha=0.5)

        ax.axis('off')
    plt.tight_layout()
    # plt.show()

def create_transparent_mask(binary_mask, save_path, foreground_color=(12, 34, 234), alpha=0.5):
    """
    Convert a binary mask to a colorful transparent mask using OpenCV.

    Args:
    binary_mask (numpy.array): A binary mask of shape (1, h, w)
    foreground_color (tuple): RGB color for the mask foreground
    alpha (float): Alpha transparency value

    Returns:
    numpy.array: An RGBA image as a numpy array
    """
    # Check input dimensions
    if binary_mask.shape[0] != 1:
        raise ValueError("Expected binary mask with shape (1, h, w)")
    binary_mask = np.uint8(binary_mask>0)

    # Remove the first dimension and create an RGB image based on the binary mask
    mask_rgb = np.zeros((*binary_mask.shape[1:], 3), dtype=np.uint8)
    mask_rgb[binary_mask[0] == 1] = foreground_color

    # Create an alpha channel based on the binary mask
    mask_alpha = (binary_mask[0] * alpha * 255).astype(np.uint8)

    # Combine the RGB and alpha channels to create an RGBA image
    mask_rgba = cv2.merge((mask_rgb[:, :, 0], mask_rgb[:, :, 1], mask_rgb[:, :, 2], mask_alpha))
    cv2.imwrite(save_path,mask_rgba)

    return mask_rgba



