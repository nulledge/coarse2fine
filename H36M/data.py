import math
import os
import pickle
import random
import torch

import cv2
import numpy as np
import torch.utils.data as torch_data
from PIL import Image
from torchvision import transforms
from vectormath import Vector2

from H36M.util import decode_image_name, rand, set_voxel
from .annotation import annotations, Annotation
from .task import tasks

T = transforms.Compose([
    transforms.ToTensor()
])


class Data(torch_data.Dataset):

    def __init__(self, annotation_path, image_path, subjects, task,
                 heatmap_xy_coefficient, voxel_xy_resolution, voxel_z_resolutions):
        self.voxel_z_res_list = voxel_z_resolutions
        self.voxel_xy_res = voxel_xy_resolution
        self.heatmap_xy_coeff = heatmap_xy_coefficient
        self.task = task
        self.subjects = subjects
        self.image_path = image_path
        self.data = dict()

        self.dummy = torch.zeros((17, 71, 64, 64))
        # self.dummy = np.zeros((len(part), z_res_cumsum[-1], self.voxel_xy_res, self.voxel_xy_res), dtype=np.float32)  # JVHW
        # self.dummy = np.zeros((17, 71, 64, 64), dtype=np.float32)  # JVHW

        for task in tasks:
            self.data[str(task)] = pickle.load(open("./%s.bin" % task, 'rb'))

    def __len__(self):
        return len(self.data[str(self.task)][str(Annotation.Image)])

    def __getitem__(self, index):
        raw_data = (self.data[str(self.task)][str(Annotation.Image)][index],)
        for annotation in annotations[str(self.task)]:
            raw_data = raw_data + (self.data[str(self.task)][str(annotation)][index],)

        image, voxels = self.preprocess(raw_data)

        return image, voxels

    def __add__(self, item):
        pass

    def preprocess(self, raw_data):
        image_name, S, center, part, scale, zind = raw_data

        # Extract subject and camera name from an image name.
        subject, _, camera, _ = decode_image_name(image_name)

        # Pre-calculate constants.
        scale = scale * 2 ** rand(0.25) * 1.25
        angle = rand(30) if random.random() <= 0.4 else 0
        image_xy_res = 200 * scale

        # Crop RGB image.

        image_path = os.path.join(self.image_path, subject, image_name)

        image = self._get_crop_image(image_path, center, scale, angle)

        coords = self._get_voxels_coords(part, center, image_xy_res, angle, zind)

        return T(image), coords

    def _get_crop_image(self, image_path, center, scale, angle, resolution=256):
        image = Image.open(image_path)

        width, height = image.size
        center = Vector2(center)  # assign new array

        # scale = scale * 1.25
        crop_ratio = 200 * scale / resolution

        if crop_ratio >= 2:  # if box size is greater than two time of resolution px
            # scale down image
            height = math.floor(height / crop_ratio)
            width = math.floor(width / crop_ratio)

            if max([height, width]) < 2:
                # Zoomed out so much that the image is now a single pixel or less
                raise ValueError("Width or height is invalid!")

            image = image.resize((width, height), Image.BILINEAR)

            center /= crop_ratio
            scale /= crop_ratio

        ul = (center - 200 * scale / 2).astype(int)
        br = (center + 200 * scale / 2).astype(int)  # Vector2

        if crop_ratio >= 2:  # force image size 256 x 256
            br -= (br - ul - resolution)

        pad_length = math.ceil(((ul - br).length - (br.x - ul.x)) / 2)

        if angle != 0:
            ul -= pad_length
            br += pad_length

        crop_src = [max(0, ul.x), max(0, ul.y), min(width, br.x), min(height, br.y)]
        crop_dst = [max(0, -ul.x), max(0, -ul.y), min(width, br.x) - ul.x, min(height, br.y) - ul.y]
        crop_image = image.crop(crop_src)

        new_image = Image.new("RGB", (br.x - ul.x, br.y - ul.y))
        new_image.paste(crop_image, box=crop_dst)

        if angle != 0:
            new_image = new_image.rotate(angle, resample=Image.BILINEAR)
            new_image = new_image.crop(box=(pad_length, pad_length,
                                            new_image.width - pad_length, new_image.height - pad_length))

        if crop_ratio < 2:
            new_image = new_image.resize((resolution, resolution), Image.BILINEAR)

        return new_image

    def _get_crop_image_cv(self, image_path, center, scale, resolution=256):
        image = cv2.imread(image_path)

        height, width, channel = image.shape
        center = Vector2(center)  # assign new array

        scale = scale * 1.25
        crop_ratio = 200 * scale / resolution

        if crop_ratio >= 2:  # if box size is greater than two time of resolution px
            # scale down image
            height = math.floor(height / crop_ratio)
            width = math.floor(width / crop_ratio)

            if max([height, width]) < 2:
                # Zoomed out so much that the image is now a single pixel or less
                raise ValueError("Width or height is invalid!")

            image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

            center /= crop_ratio
            scale /= crop_ratio

        ul = (center - 200 * scale / 2).astype(int)
        br = (center + 200 * scale / 2).astype(int)  # Vector2

        if crop_ratio >= 2:  # force image size 256 x 256
            br -= (br - ul - resolution)

        src = [max(0, ul.y), min(height, br.y), max(0, ul.x), min(width, br.x)]
        dst = [max(0, -ul.y), min(height, br.y) - ul.y, max(0, -ul.x), min(width, br.x) - ul.x]
        new_image = np.zeros([br.y - ul.y, br.x - ul.x, channel], dtype=np.uint8)
        new_image[dst[0]:dst[1], dst[2]:dst[3], :] = image[src[0]:src[1], src[2]:src[3], :]

        if crop_ratio < 2:
            new_image = cv2.resize(new_image, (resolution, resolution), interpolation=cv2.INTER_AREA)

        return new_image

    def _get_voxels(self, part, center, image_xy_res, angle, zind):
        z_res_cumsum = np.insert(np.cumsum(self.voxel_z_res_list), 0, 0)

        # Build voxel.
        voxel_z_fine_res = self.voxel_z_res_list[-1]
        for idx, z_res in enumerate(self.voxel_z_res_list):
            # heatmap_z_coefficient is 1, 1, 1, 3, 5, 7, 13 for 1, 2, 4, 8, 16, 32, 64.
            heatmap_z_coeff = 2 * math.floor((6 * self.heatmap_xy_coeff * z_res / voxel_z_fine_res + 1) / 2) + 1

            # Convert the coordinate from a RGB image to a cropped RGB image.
            xy = self.voxel_xy_res * (part - center) / image_xy_res + self.voxel_xy_res * 0.5

            if angle != 0.0:
                xy = xy - self.voxel_xy_res / 2
                cos = math.cos(angle * math.pi / 180)
                sin = math.sin(angle * math.pi / 180)
                x = sin * xy[:, 1] + cos * xy[:, 0]
                y = cos * xy[:, 1] - sin * xy[:, 0]
                xy[:, 0] = x
                xy[:, 1] = y
                xy = xy + self.voxel_xy_res / 2

            voxel = voxels[:, z_res_cumsum[idx]:z_res_cumsum[idx + 1], :, :]
            for part_idx in range(len(part)):
                # zind range (1, 64)
                # z range (0, 63)
                z = math.ceil(zind[part_idx] * z_res / voxel_z_fine_res) - 1
                if xy[part_idx, 0] < 0 or self.voxel_xy_res <= xy[part_idx, 0] or \
                        xy[part_idx, 1] < 0 or self.voxel_xy_res <= xy[part_idx, 1]:
                    continue
                set_voxel(voxel[part_idx, :, :, :],
                          self.voxel_xy_res,
                          z_res,
                          xy[part_idx],
                          z,
                          self.heatmap_xy_coeff,
                          heatmap_z_coeff)

        return voxels

    def _get_voxels_coords(self, part, center, image_xy_res, angle, zind):
        xy = self.voxel_xy_res * (part - center) / image_xy_res + self.voxel_xy_res * 0.5

        if angle != 0.0:
            xy = xy - self.voxel_xy_res / 2
            cos = math.cos(angle * math.pi / 180)
            sin = math.sin(angle * math.pi / 180)
            x = sin * xy[:, 1] + cos * xy[:, 0]
            y = cos * xy[:, 1] - sin * xy[:, 0]
            xy[:, 0] = x
            xy[:, 1] = y
            xy = xy + self.voxel_xy_res / 2

        coords = np.concatenate((xy, zind[:, np.newaxis]), axis=1)

        return coords
        # for idx, z_res in enumerate(self.voxel_z_res_list):
            # Convert the coordinate from a RGB image to a cropped RGB image.


            # for part_idx in range(len(part)):
                # if xy[part_idx, 0] < 0 or self.voxel_xy_res <= xy[part_idx, 0] or \
                #         xy[part_idx, 1] < 0 or self.voxel_xy_res <= xy[part_idx, 1]:
                #     continue
                # set_voxel(voxel[part_idx, :, :, :],
                #           self.voxel_xy_res,
                #           z_res,
                #           xy[part_idx],
                #           z,
                #           self.heatmap_xy_coeff,
                #           heatmap_z_coeff)

