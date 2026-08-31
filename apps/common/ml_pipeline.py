import os
import logging

logger = logging.getLogger(__name__)

# Try to import torch, fail gracefully if not installed so app doesn't crash
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.warning("PyTorch not installed. ML Pipeline will be unavailable.")

if TORCH_AVAILABLE:
    class CNNModule(nn.Module):
        """
        Lightweight Convolutional Neural Network (CNN) for initial delamination segmentation.
        Equivalent to the IDSNet architecture specified in Phase 2 documentation.
        """
        def __init__(self):
            super(CNNModule, self).__init__()
            # Lightweight segmentation architecture (~0.085M parameters)
            self.conv1 = nn.Conv2d(1, 16, kernel_size=3, padding=1)
            self.conv2 = nn.Conv2d(16, 32, kernel_size=3, padding=1)
            self.conv3 = nn.Conv2d(32, 1, kernel_size=3, padding=1)

        def forward(self, x):
            x = F.relu(self.conv1(x))
            x = F.max_pool2d(x, 2)
            x = F.relu(self.conv2(x))
            x = F.max_pool2d(x, 2)
            x = torch.sigmoid(self.conv3(x))
            return x

    class SNNModule(nn.Module):
        """
        Siamese Neural Network (SNN) for false-positive filtering.
        Compares thermal ROI against corresponding visible ROI.
        """
        def __init__(self):
            super(SNNModule, self).__init__()
            # Feature extractor
            self.feature_extractor = nn.Sequential(
                nn.Conv2d(1, 16, kernel_size=3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(16, 32, kernel_size=3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2)
            )
            # Fully connected layers (assuming input ROI is scaled to 64x64)
            self.fc = nn.Sequential(
                nn.Linear(32 * 16 * 16, 128),
                nn.ReLU(),
                nn.Linear(128, 64)
            )

        def forward_one(self, x):
            x = self.feature_extractor(x)
            x = x.view(x.size()[0], -1)
            x = self.fc(x)
            return x

        def forward(self, thermal_roi, visible_roi):
            out1 = self.forward_one(thermal_roi)
            out2 = self.forward_one(visible_roi)
            # Euclidean distance
            distance = F.pairwise_distance(out1, out2)
            return distance

    class MultimodalPipeline:
        """
        Two-stage pipeline:
        1. CNN detects potential delamination in thermal images.
        2. SNN compares the suspect region with visible image area.
        """
        def __init__(self, cnn_weights_path=None, snn_weights_path=None):
            self.cnn = CNNModule()
            self.snn = SNNModule()
            self.is_loaded = False
            
            try:
                if cnn_weights_path and os.path.exists(cnn_weights_path):
                    self.cnn.load_state_dict(torch.load(cnn_weights_path, map_location=torch.device('cpu')))
                    logger.info("Loaded CNN weights.")
                if snn_weights_path and os.path.exists(snn_weights_path):
                    self.snn.load_state_dict(torch.load(snn_weights_path, map_location=torch.device('cpu')))
                    logger.info("Loaded SNN weights.")
                
                # We consider it loaded if both paths are provided and exist
                if cnn_weights_path and snn_weights_path and os.path.exists(cnn_weights_path) and os.path.exists(snn_weights_path):
                    self.cnn.eval()
                    self.snn.eval()
                    self.is_loaded = True
            except Exception as e:
                logger.error(f"Failed to load ML models: {e}")
                self.is_loaded = False

        def process_images(self, thermal_url, visible_url):
            """
            Process a registered pair of thermal and visible images.
            Returns a list of structured delamination objects or None if not loaded.
            """
            if not self.is_loaded:
                return None
            
            try:
                import requests
                from PIL import Image
                from io import BytesIO
                import torchvision.transforms as transforms
                
                resp_t = requests.get(thermal_url)
                resp_v = requests.get(visible_url)
                if resp_t.status_code != 200 or resp_v.status_code != 200:
                    logger.error("Failed to download images for multimodal processing.")
                    return None
                    
                img_t = Image.open(BytesIO(resp_t.content)).convert('L')
                img_v = Image.open(BytesIO(resp_v.content)).convert('L')
                
                transform = transforms.Compose([
                    transforms.Resize((480, 640)),
                    transforms.ToTensor()
                ])
                
                t_tensor = transform(img_t).unsqueeze(0)
                v_tensor = transform(img_v).unsqueeze(0)
                
                with torch.no_grad():
                    cnn_out = self.cnn(t_tensor)
                    
                threshold = 0.5
                mask = (cnn_out > threshold).float()
                
                detected_delaminations = []
                
                H, W = mask.shape[2], mask.shape[3]
                grid_size = 16
                snn_threshold = 1.0
                
                for i in range(0, H - grid_size, grid_size):
                    for j in range(0, W - grid_size, grid_size):
                        roi_mask = mask[0, 0, i:i+grid_size, j:j+grid_size]
                        
                        if roi_mask.sum() > (grid_size * grid_size * 0.3):
                            orig_i = i * 4
                            orig_j = j * 4
                            orig_grid = grid_size * 4
                            
                            t_roi = t_tensor[:, :, orig_i:orig_i+orig_grid, orig_j:orig_j+orig_grid]
                            v_roi = v_tensor[:, :, orig_i:orig_i+orig_grid, orig_j:orig_j+orig_grid]
                            
                            roi_transform = transforms.Resize((64, 64))
                            t_roi_resized = roi_transform(t_roi)
                            v_roi_resized = roi_transform(v_roi)
                            
                            with torch.no_grad():
                                distance = self.snn(t_roi_resized, v_roi_resized).item()
                                
                            is_false_positive = distance < snn_threshold
                            
                            if not is_false_positive:
                                detected_delaminations.append({
                                    "type": "delamination",
                                    "severity": "high",
                                    "description": "Subsurface delamination confirmed via multimodal SNN verification.",
                                    "location_x": float(orig_j),
                                    "location_y": float(orig_i),
                                    "location_z": 0.0,
                                    "confidence_score": 0.85,
                                    "is_false_positive": False
                                })
                            else:
                                detected_delaminations.append({
                                    "type": "delamination",
                                    "severity": "low",
                                    "description": "Surface stain/debris mimicking delamination (False Positive).",
                                    "location_x": float(orig_j),
                                    "location_y": float(orig_i),
                                    "location_z": 0.0,
                                    "confidence_score": 0.20,
                                    "is_false_positive": True
                                })
                                
                return detected_delaminations
                
            except Exception as e:
                logger.error(f"Error in multimodal image processing: {e}")
                return None
else:
    class MultimodalPipeline:
        def __init__(self, *args, **kwargs):
            self.is_loaded = False
            
        def process_images(self, thermal_url, visible_url):
            return None
