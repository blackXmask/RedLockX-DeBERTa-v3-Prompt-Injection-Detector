import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import joblib

from transformers import AutoTokenizer, AutoModel
from typing import Dict, List, Any


# =========================================================
# 1. Multi-Task Architecture
# =========================================================
class MultiTaskModel(nn.Module):

    def __init__(self, model_name, num_fine, num_family):
        super().__init__()

        self.encoder = AutoModel.from_pretrained(model_name)
        hidden = self.encoder.config.hidden_size

        self.dropout = nn.Dropout(0.2)

        self.binary_head = nn.Linear(hidden, 1)
        self.multi_head = nn.Linear(hidden, num_fine)
        self.family_head = nn.Linear(hidden, num_family)

    def mean_pooling(self, hidden, attention_mask):
        mask = attention_mask.unsqueeze(-1).float()
        return (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)

    def forward(self, input_ids, attention_mask):
        outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask
        )

        pooled = self.mean_pooling(
            outputs.last_hidden_state,
            attention_mask
        )

        x = self.dropout(pooled)

        return (
            self.binary_head(x),
            self.multi_head(x),
            self.family_head(x)
        )


# =========================================================
# 2. Hugging Face Endpoint Handler
# =========================================================
class EndpointHandler:

    def __init__(self, path=""):

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )

        print(f"[INFO] Using device: {self.device}")

        # Load encoders
        self.fine_le = joblib.load(os.path.join(path, "fine_encoder.pkl"))
        self.family_le = joblib.load(os.path.join(path, "family_encoder.pkl"))

        # Tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(path)

        # Model
        self.model = MultiTaskModel(
            model_name="microsoft/deberta-v3-small",
            num_fine=len(self.fine_le.classes_),
            num_family=len(self.family_le.classes_)
        ).to(self.device)

        checkpoint = torch.load(
            os.path.join(path, "multitask_model_FINAL.pt"),
            map_location=self.device
        )

        state_dict = checkpoint.get("model_state", checkpoint)
        self.model.load_state_dict(state_dict)

        self.model.eval()

        print("[INFO] Model loaded successfully")

        # ============================================
        # Threshold Config — TIGHTENED
        # ============================================
        self.config = {
            "HIGH_ATTACK": 0.80,      # ← RAISED from 0.75
            "MEDIUM_ATTACK": 0.55,
            "HIGH_CONF": 0.85,
            "LOW_CONF": 0.30,
            "EXTREME_CONF": 0.95
        }

    # =====================================================
    # Decision Logic
    # =====================================================
    def decide(self, danger_prob, fine_score, family_score):
        """
        Binary head (danger_prob) is the authority.
        Attack type scores only matter if binary head is already suspicious.
        """
        cfg = self.config

        # 1. Definite safe zone
        if danger_prob <= cfg["LOW_CONF"]:
            return False

        # 2. Danger zone — binary head must be confident it's dangerous
        if danger_prob >= cfg["HIGH_CONF"]:
            # Strong attack type confidence → dangerous
            if fine_score >= cfg["HIGH_ATTACK"] or family_score >= cfg["HIGH_ATTACK"]:
                return True

            # Medium attack type confidence → dangerous only if binary is EXTREMELY confident
            if fine_score >= cfg["MEDIUM_ATTACK"] or family_score >= cfg["MEDIUM_ATTACK"]:
                if danger_prob >= cfg["EXTREME_CONF"]:
                    return True

        # 3. Gray zone — always safe
        return False

    # =====================================================
    # Predict Single
    # =====================================================
    def predict_single(self, text: str):

        tokenized = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=512
        )

        tokenized = {k: v.to(self.device) for k, v in tokenized.items()}

        with torch.no_grad():

            binary_logits, multi_logits, family_logits = self.model(
                tokenized["input_ids"],
                tokenized["attention_mask"]
            )

            # ================================
            # Probabilities
            # ================================
            danger_prob = torch.sigmoid(binary_logits).item()

            multi_probs = F.softmax(multi_logits, dim=1)
            family_probs = F.softmax(family_logits, dim=1)

            fine_idx = torch.argmax(multi_probs, dim=1).item()
            family_idx = torch.argmax(family_probs, dim=1).item()

            fine_score = multi_probs[0][fine_idx].item()
            family_score = family_probs[0][family_idx].item()

            # ================================
            # Decision
            # ================================
            is_dangerous = self.decide(
                danger_prob,
                fine_score,
                family_score
            )

            # ================================
            # Labels — only predict attack type if dangerous
            # ================================
            if is_dangerous:
                attack_type = self.fine_le.inverse_transform([fine_idx])[0]
                attack_family = self.family_le.inverse_transform([family_idx])[0]
            else:
                attack_type = "none"
                attack_family = "none"
                fine_score = 0.0
                family_score = 0.0

        # ================================
        # Explainability
        # ================================
        suspicious_keywords = [
            "ignore", "override", "reveal", "system prompt",
            "developer mode", "bypass", "disable",
            "forget instructions", "pretend", "simulate", "jailbreak"
        ]

        found_keywords = [
            kw for kw in suspicious_keywords
            if kw in text.lower()
        ]

        # ================================
        # Response
        # ================================
        return {
            "status": "DANGEROUS" if is_dangerous else "SAFE",

            "binary_confidence": round(danger_prob, 4),

            "confidence": round(
                danger_prob if is_dangerous else (1 - danger_prob),
                4
            ),

            "attack_type": {
                "label": attack_type,
                "score": round(fine_score, 4)
            },

            "attack_family": {
                "label": attack_family,
                "score": round(family_score, 4)
            },

            "trigger_words": found_keywords
        }

    # =====================================================
    # Main Entry
    # =====================================================
    def __call__(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:

        inputs = data["inputs"] if isinstance(data, dict) else data

        if isinstance(inputs, str):
            inputs = [inputs]

        return [self.predict_single(text) for text in inputs]