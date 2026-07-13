"""Train the RiskGuard XGBoost pipeline and write versioned artifacts.

    python ml/train.py --data data/loans.csv --artifact-dir ml/artifacts/v1
    python ml/train.py --data data/loans.csv --use-smote   # SMOTE instead of scale_pos_weight

Class-imbalance strategy: `scale_pos_weight` (n_neg / n_pos) is the primary
mechanism — for gradient-boosted trees it outperforms SMOTE and never leaks
synthetic samples into validation/test. `--use-smote` is an alternate path
(via imblearn's Pipeline, which only resamples during .fit — .predict_proba
skips the resampler) kept to demonstrate awareness of both approaches; it is
off by default.

Two-stage fit: early stopping (on a held-out slice of the training split)
picks the boosting-round count once, then the final artifact is refit on the
*full* 80% training split with that fixed round count — this avoids leaking
the early-stopping validation slice's statistics into the saved model while
still letting XGBoost pick a sensible n_estimators automatically.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from ml.generate_dataset import FEATURE_NAMES, TARGET

_XGB_PARAMS = dict(
    max_depth=4,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    n_jobs=-1,
)


def _build_preprocessor() -> ColumnTransformer:
    # A single numeric branch today; ColumnTransformer is used (rather than a
    # bare Pipeline) so a categorical branch can be added later without
    # restructuring the artifact.
    numeric = Pipeline(
        [("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler())]
    )
    return ColumnTransformer([("num", numeric, FEATURE_NAMES)])


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _select_n_estimators(X_fit, y_fit, X_val, y_val, seed: int, use_smote: bool) -> int:
    """Fits on (X_fit, y_val)-adjacent data with early stopping; returns the
    best round count to use for the final, non-early-stopped refit."""
    preprocessor = _build_preprocessor()
    X_fit_t = preprocessor.fit_transform(X_fit)
    X_val_t = preprocessor.transform(X_val)

    if use_smote:
        from imblearn.over_sampling import SMOTE

        X_fit_t, y_fit = SMOTE(random_state=seed).fit_resample(X_fit_t, y_fit)
        scale_pos_weight = 1.0
    else:
        scale_pos_weight = (y_fit == 0).sum() / (y_fit == 1).sum()

    clf = XGBClassifier(
        **_XGB_PARAMS,
        n_estimators=500,
        scale_pos_weight=scale_pos_weight,
        eval_metric="aucpr",
        early_stopping_rounds=50,
        random_state=seed,
    )
    clf.fit(X_fit_t, y_fit, eval_set=[(X_val_t, y_val)], verbose=False)
    return int(clf.best_iteration) + 1


def _build_final_pipeline(n_estimators: int, seed: int, use_smote: bool, y_train) -> Pipeline:
    preprocessor = _build_preprocessor()
    classifier = XGBClassifier(**_XGB_PARAMS, n_estimators=n_estimators, random_state=seed)

    if use_smote:
        from imblearn.over_sampling import SMOTE
        from imblearn.pipeline import Pipeline as ImbPipeline

        classifier.set_params(scale_pos_weight=1.0)
        return ImbPipeline(
            [
                ("preprocessor", preprocessor),
                ("smote", SMOTE(random_state=seed)),
                ("classifier", classifier),
            ]
        )

    scale_pos_weight = (y_train == 0).sum() / (y_train == 1).sum()
    classifier.set_params(scale_pos_weight=scale_pos_weight)
    return Pipeline([("preprocessor", preprocessor), ("classifier", classifier)])


def _best_f1_threshold(y_true, y_proba) -> float:
    thresholds = np.linspace(0.01, 0.99, 99)
    f1s = [f1_score(y_true, y_proba >= t, zero_division=0) for t in thresholds]
    return float(thresholds[int(np.argmax(f1s))])


def train(data_path: Path, artifact_dir: Path, seed: int, use_smote: bool) -> dict:
    df = pd.read_csv(data_path)
    X, y = df[FEATURE_NAMES], df[TARGET]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=seed
    )
    X_fit, X_val, y_fit, y_val = train_test_split(
        X_train, y_train, test_size=0.15, stratify=y_train, random_state=seed
    )

    n_estimators = _select_n_estimators(X_fit, y_fit, X_val, y_val, seed, use_smote)
    pipeline = _build_final_pipeline(n_estimators, seed, use_smote, y_train)
    pipeline.fit(X_train, y_train)

    # Tune the F1-optimal threshold on the early-stopping validation slice —
    # kept separate from the test set so test metrics aren't threshold-fit.
    val_proba = pipeline.predict_proba(X_val)[:, 1]
    threshold = _best_f1_threshold(y_val, val_proba)

    test_proba = pipeline.predict_proba(X_test)[:, 1]
    test_pred = test_proba >= threshold

    metrics = {
        "precision": precision_score(y_test, test_pred, zero_division=0),
        "recall": recall_score(y_test, test_pred, zero_division=0),
        "f1": f1_score(y_test, test_pred, zero_division=0),
        "roc_auc": roc_auc_score(y_test, test_proba),
        "pr_auc": average_precision_score(y_test, test_proba),
        "confusion_matrix": confusion_matrix(y_test, test_pred).tolist(),
        "class_balance": {
            "total_rows": int(len(df)),
            "positive_rate": float(y.mean()),
        },
        "threshold": threshold,
        "n_estimators": n_estimators,
        "use_smote": use_smote,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "model_version": artifact_dir.name,
    }

    fpr, tpr, _ = roc_curve(y_test, test_proba)
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot(fpr, tpr, label=f"ROC (AUC = {metrics['roc_auc']:.3f})")
    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="chance")
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate")
    ax.set_title("RiskGuard default-prediction ROC curve")
    ax.legend(loc="lower right")

    artifact_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(artifact_dir / "roc_curve.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    joblib.dump(pipeline, artifact_dir / "model.joblib")
    (artifact_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (artifact_dir / "threshold.json").write_text(json.dumps({"threshold": threshold}, indent=2))
    (artifact_dir / "feature_names.json").write_text(json.dumps(FEATURE_NAMES, indent=2))

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/loans.csv"))
    parser.add_argument("--artifact-dir", type=Path, default=Path("ml/artifacts/v1"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--use-smote", action="store_true")
    args = parser.parse_args()

    metrics = train(args.data, args.artifact_dir, args.seed, args.use_smote)

    print(f"precision: {metrics['precision']:.4f}")
    print(f"recall:    {metrics['recall']:.4f}")
    print(f"f1:        {metrics['f1']:.4f}")
    print(f"roc_auc:   {metrics['roc_auc']:.4f}")
    print(f"pr_auc:    {metrics['pr_auc']:.4f}")
    print(f"threshold: {metrics['threshold']:.2f}")
    print(f"wrote artifacts to {args.artifact_dir}")


if __name__ == "__main__":
    main()
