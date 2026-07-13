"""Trains on a small synthetic sample (not the full 25k-row dataset) and
asserts the pipeline produces well-formed metrics and artifacts. This is a
correctness/wiring check, not a model-quality gate — see ml/train.py's own
stdout (or metrics.json from a full run) for the real AUC bar (>0.80)."""

from __future__ import annotations

import json
from pathlib import Path

from ml.generate_dataset import generate_dataset
from ml.predict import LoanFeatures, RiskModel
from ml.train import train


def test_train_pipeline_produces_valid_artifacts(tmp_path: Path):
    data_path = tmp_path / "loans.csv"
    df = generate_dataset(n_rows=3000, seed=1, target_default_rate=0.08)
    df.to_csv(data_path, index=False)

    artifact_dir = tmp_path / "artifacts"
    metrics = train(data_path, artifact_dir, seed=1, use_smote=False)

    for key in ("precision", "recall", "f1", "roc_auc", "pr_auc", "threshold"):
        assert 0.0 <= metrics[key] <= 1.0, f"{key}={metrics[key]!r} out of [0, 1]"
    assert metrics["roc_auc"] > 0.5, "model should beat random guessing"
    assert metrics["class_balance"]["total_rows"] == 3000
    assert 0.0 < metrics["class_balance"]["positive_rate"] < 0.20

    for filename in ("model.joblib", "metrics.json", "threshold.json", "feature_names.json", "roc_curve.png"):
        assert (artifact_dir / filename).exists(), f"missing artifact: {filename}"

    saved_metrics = json.loads((artifact_dir / "metrics.json").read_text())
    assert saved_metrics["roc_auc"] == metrics["roc_auc"]


def test_train_pipeline_with_smote_produces_valid_artifacts(tmp_path: Path):
    data_path = tmp_path / "loans.csv"
    df = generate_dataset(n_rows=3000, seed=2, target_default_rate=0.08)
    df.to_csv(data_path, index=False)

    artifact_dir = tmp_path / "artifacts_smote"
    metrics = train(data_path, artifact_dir, seed=2, use_smote=True)

    assert metrics["use_smote"] is True
    assert 0.0 <= metrics["roc_auc"] <= 1.0
    assert (artifact_dir / "model.joblib").exists()


def test_risk_model_scores_high_and_low_risk_profiles_correctly(tmp_path: Path):
    data_path = tmp_path / "loans.csv"
    df = generate_dataset(n_rows=5000, seed=3, target_default_rate=0.08)
    df.to_csv(data_path, index=False)

    artifact_dir = tmp_path / "artifacts"
    train(data_path, artifact_dir, seed=3, use_smote=False)

    model = RiskModel(artifact_dir)
    assert model.model_version == artifact_dir.name

    high_risk = LoanFeatures(
        credit_score=550,
        debt_to_income=0.60,
        payment_history=8,
        loan_amount=400_000,
        employment_duration=0.2,
    )
    low_risk = LoanFeatures(
        credit_score=800,
        debt_to_income=0.10,
        payment_history=0,
        loan_amount=20_000,
        employment_duration=15,
    )
    assert model.predict_default_probability(high_risk) > model.predict_default_probability(low_risk)

    # Missing features must not raise — the imputer handles them.
    partial = LoanFeatures(credit_score=650, loan_amount=50_000)
    proba = model.predict_default_probability(partial)
    assert 0.0 <= proba <= 1.0
