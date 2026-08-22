#!/usr/bin/env python3
import inspect
import unittest
import argparse
import torch
import torch.nn.functional as F
import numpy as np
import os
import sys
import json
import re
from graderUtil import graded, CourseTestRunner, GradedTestCase
from unittest.mock import MagicMock, patch
from PIL import Image

from autograde_utils import if_text_in_py, text_in_cell, assert_allclose


# import student submission
import submission

#########
# HELPERS #
#########


class MockDavisDataset:
    """
    A mock Davis dataset that uses synthetic data for testing
    """

    def __init__(self, num_frames=5, frame_size=224):
        self.num_frames = num_frames
        self.frame_size = frame_size

        # Create synthetic data
        self.frames = [
            torch.randn(3, frame_size, frame_size) for _ in range(num_frames)
        ]
        self.masks = [
            torch.randint(0, 2, (1, frame_size, frame_size)) for _ in range(num_frames)
        ]

    def get_sample(self, index):
        return self.frames, self.masks


class MockModel(torch.nn.Module):
    """
    A mock model for testing
    """
    def __init__(self):
        super().__init__()
        self.linear = torch.nn.Linear(10, 10)

    def forward(self, x, t, model_kwargs={}):
        return x  # Just return the input for simplicity


class MockLossModel(torch.nn.Module):
    """
    Create a custom mock model that returns a defined value
    """
    def __init__(self):
        super().__init__()

    def forward(self, x, t, model_kwargs={}):
        return (
            torch.ones_like(x) * 0.5
        )  # Return constant value for predictable testing


class MockSampleModel(torch.nn.Module):

    """
    # Create a custom mock model that returns a defined value
    """
    def __init__(self):
        super().__init__()

    def forward(self, x, t, model_kwargs={}):
        return torch.zeros_like(x)  # Return zeros for predictable testing


#########
# TESTS #
#########


class Test_1(GradedTestCase):
    """
    DDPM tests
    """

    def setUp(self):

        torch.manual_seed(231)
        np.random.seed(231)

        # Initialize solution and submission diffusion models
        self.sol_model = self.run_with_solution_if_possible(
            submission,
            lambda sub_or_sol: sub_or_sol.GaussianDiffusion(
                MockModel(),
                image_size=32,
                timesteps=100,
                objective="pred_noise",
                beta_schedule="sigmoid",
            ),
        )

        self.sub_model = submission.GaussianDiffusion(
            MockModel(),
            image_size=32,
            timesteps=100,
            objective="pred_noise",
            beta_schedule="sigmoid",
        )

        # Initialize UNet models for testing
        self.sol_unet = self.run_with_solution_if_possible(
            submission,
            lambda sub_or_sol: sub_or_sol.Unet(
                dim=32, channels=3, dim_mults=(1, 2, 4), condition_dim=512
            ),
        )

        self.sub_unet = submission.Unet(
            dim=32, channels=3, dim_mults=(1, 2, 4), condition_dim=512
        )

        # Path to the notebook
        self.notebook_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "submission",
            "DDPM.ipynb",
        )

    ### BEGIN_HIDE ###
    ### END_HIDE ###


class Test_2(GradedTestCase):
    """
    CLIP tests
    """

    def setUp(self):
        # Set up common resources for tests
        if torch.backends.mps.is_available() and torch.backends.mps.is_built():
            self.device = torch.device("mps")
        elif torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")

        # Create mock tensors for text and image features
        self.text_features = torch.randn(1, 512, device=self.device)  # 512-dim features
        self.text_features = self.text_features / self.text_features.norm(
            dim=1, keepdim=True
        )

        self.image_features = torch.randn(
            10, 512, device=self.device
        )  # 10 images, 512-dim features
        self.image_features = self.image_features / self.image_features.norm(
            dim=1, keepdim=True
        )

        # Create mock CLIP model
        self.mock_clip_model = MagicMock()

        # ------------------------------------------------------------------
        # Make the mock outputs depend *deterministically* on the inputs so
        # that the behaviour is robust to different student implementations.
        # ------------------------------------------------------------------
        # --- encode_text ----------------------------------------------------
        def _encode_text_side_effect(token_tensor):
            # token_tensor: shape (N, L). Create deterministic but varied features
            # by using the sum of token ids to seed random generation
            N = token_tensor.shape[0]
            features = []
            for i in range(N):
                # Use sum of tokens for this text as seed for deterministic random generation
                seed = int(token_tensor[i].sum().item()) % (2**31 - 1)
                torch.manual_seed(seed)
                feat = torch.randn(512, device=token_tensor.device)
                features.append(feat)
            feats = torch.stack(features)
            return F.normalize(feats, dim=1)

        self.mock_clip_model.encode_text.side_effect = _encode_text_side_effect

        # --- encode_image ---------------------------------------------------
        def _encode_image_side_effect(img_tensor):
            # img_tensor: shape (N, 3, H, W). Create deterministic but varied features
            # by using image content to seed random generation
            N = img_tensor.shape[0]
            features = []
            for i in range(N):
                # Use mean of image pixels as seed for deterministic random generation
                seed = int((img_tensor[i].mean() * 1000000).item()) % (2**31 - 1)
                torch.manual_seed(seed)
                feat = torch.randn(512, device=img_tensor.device)
                features.append(feat)
            feats = torch.stack(features)
            return F.normalize(feats, dim=1)

        self.mock_clip_model.encode_image.side_effect = _encode_image_side_effect

        # --- forward ( __call__ ) ------------------------------------------
        def _forward_side_effect(img_tensor, text_tokens):
            img_feat = _encode_image_side_effect(img_tensor)  # (N,512)
            text_feat = _encode_text_side_effect(text_tokens)  # (M,512)
            logits_per_image = img_feat @ text_feat.T  # (N,M)
            logits_per_text = text_feat @ img_feat.T  # (M,N)
            return logits_per_image, logits_per_text

        self.mock_clip_model.side_effect = _forward_side_effect

        # Set up mock preprocessor that returns different tensors based on input
        def _preprocess_side_effect(pil_image):
            # Convert PIL image back to numpy to get a deterministic hash
            img_array = np.array(pil_image)
            # Use image content hash as seed for deterministic random generation
            seed = int(np.sum(img_array).item()) % (2**31 - 1)
            torch.manual_seed(seed)
            return torch.randn(3, 224, 224)

        self.mock_preprocess = MagicMock(side_effect=_preprocess_side_effect)

        # Create mock images - use PIL images or numpy arrays compatible with PIL
        self.mock_images = []
        np.random.seed(42)  # Set fixed seed for deterministic mock images
        for i in range(10):
            # Create valid PIL-compatible numpy array (HWC format, uint8 type)
            # Use different patterns for each image to ensure variety
            img_array = np.random.randint(
                i * 20, (i + 1) * 20 + 50, (224, 224, 3), dtype=np.uint8
            )
            self.mock_images.append(img_array)

        self.class_texts = ["class1", "class2", "class3", "class4", "class5"]

        # Mock DINO model
        self.mock_dino_model = MagicMock()
        # Set up mock DINO output
        self.mock_dino_output = [
            torch.randn(1, 196, 384) for _ in range(5)
        ]  # Assuming 14x14 patches with 384 features
        self.mock_dino_model.get_intermediate_layers.return_value = [
            {"x": patch} for patch in self.mock_dino_output
        ]

        # Mock dataset frames and masks
        self.mock_frames = [torch.randn(3, 224, 224) for _ in range(5)]
        self.mock_masks = [torch.randint(0, 2, (1, 224, 224)) for _ in range(5)]

        # Create a full mock dataset
        self.mock_davis_dataset = MockDavisDataset()

    ### BEGIN_HIDE ###
    ### END_HIDE ###
    @graded()
    def test_3(self):
        """2-3-basic: IOU accuracy"""

        # Path to student's notebook
        notebook_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "submission",
            "CLIP_DINO.ipynb",
        )

        try:
            # Get the output from the tagged cell
            block_text = text_in_cell(notebook_path, "iou_accuracy")

            # Extract IoU values
            first_iou = None
            last_iou = None

            for line in block_text:
                first_match = re.search(
                    r"Mean IoU on first test frames: ([\d\.]+)", line
                )
                last_match = re.search(r"Mean IoU on last test frames: ([\d\.]+)", line)

                if first_match:
                    first_iou = float(first_match.group(1))
                if last_match:
                    last_iou = float(last_match.group(1))

            # Check if we found both values
            if first_iou is None or last_iou is None:
                self.fail("Could not extract IoU values")

            # Check against requirements
            self.assertGreaterEqual(
                first_iou,
                0.45,
                f"Mean IoU on first test frames ({first_iou:.3f}) is below required threshold (0.45)",
            )
            self.assertGreaterEqual(
                last_iou,
                0.50,
                f"Mean IoU on last test frames ({last_iou:.3f}) is below required threshold (0.50)",
            )

        except Exception as e:
            self.fail(f"Error testing IoU accuracy: {str(e)}")

    @graded()
    def test_4(self):
        """2-4-basic: All frames IOU"""

        # Path to student's notebook
        notebook_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "submission",
            "CLIP_DINO.ipynb",
        )

        try:
            # Get the output from the tagged cell
            block_text = text_in_cell(notebook_path, "all_frames_iou")

            # Extract IoU value
            first_frames_iou = None
            for line in block_text:
                match = re.search(r"Mean IoU on all frames: ([\d\.]+)", line)
                if match:
                    first_frames_iou = float(match.group(1))
                    break

            # Check if we found the value
            if first_frames_iou is None:
                self.fail("Could not find Mean IoU value")

            # Check against higher requirement
            self.assertGreaterEqual(
                first_frames_iou,
                0.55,
                f"Mean IoU on all frames ({first_frames_iou:.3f}) is below the higher threshold (0.55)",
            )

        except Exception as e:
            self.fail(f"Error testing first frames IoU: {str(e)}")



def getTestCaseForTestID(test_id):
    question, part, _ = test_id.split("-")
    g = globals().copy()
    for name, obj in g.items():
        if inspect.isclass(obj) and name == ("Test_" + question):
            return obj("test_" + part)

if __name__ == "__main__":
    # Parse for a specific test
    parser = argparse.ArgumentParser()
    parser.add_argument("test_case", nargs="?", default="all")
    test_id = parser.parse_args().test_case

    assignment = unittest.TestSuite()
    if test_id != "all":
        assignment.addTest(getTestCaseForTestID(test_id))
    else:
        assignment.addTests(
            unittest.defaultTestLoader.discover(".", pattern="grader.py")
        )
    CourseTestRunner().run(assignment)
