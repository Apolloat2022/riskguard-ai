"""Load-once inference wrapper around the trained joblib pipeline.

XGBoost inference on a single row is sub-millisecond and CPU-bound, so the
risk endpoint calls RiskModel.predict_default_probability directly in the
request path — no thread-pool/executor ceremony is needed for single-row
scoring (that overhead would only pay off for batch scoring).
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import pandas as pd
from pydantic import BaseModel

from app.exceptions import ModelInferenceError


class LoanFeatures(BaseModel):
    """Mirrors the columns produced by the risk-assessment CTE (app/db/queries.py).
    Fields are optional because the imputer in the trained pipeline is designed
    to handle missing values — callers should pass None rather than guessing.
    """

    credit_score: float | None = None
    debt_to_income: float | None = None
    payment_history: float | None = None
    loan_amount: float | None = None
    employment_duration: float | None = None


class RiskModel:
    """Loads the joblib pipeline once; thread-safe read-only inference."""

    def __init__(self, artifact_dir: Path):
        self._artifact_dir = Path(artifact_dir)
        try:
            self._pipeline = joblib.load(self._artifact_dir / "model.joblib")
            self._feature_names: list[str] = json.loads(
                (self._artifact_dir / "feature_names.json").read_text()
            )
            self._threshold: float = json.loads(
                (self._artifact_dir / "threshold.json").read_text()
            )["threshold"]
            self.metrics: dict = json.loads((self._artifact_dir / "metrics.json").read_text())
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise ModelInferenceError(f"failed to load model artifacts from {artifact_dir}: {exc}") from exc

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def model_version(self) -> str:
        return self.metrics.get("model_version", self._artifact_dir.name)

    def predict_default_probability(self, features: LoanFeatures) -> float:
        """Returns p(default) in [0.0, 1.0]. Raises ModelInferenceError on failure."""
        try:
            row = features.model_dump()
            df = pd.DataFrame([row], columns=self._feature_names)
            proba = self._pipeline.predict_proba(df)[0, 1]
            return float(proba)
        except Exception as exc:  # noqa: BLE001 - any inference failure is a ModelInferenceError
            raise ModelInferenceError(f"inference failed: {exc}") from exc
