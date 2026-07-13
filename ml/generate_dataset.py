"""Synthetic loan-portfolio dataset generator.

Produces realistic, *imbalanced* (~8% default rate) tabular data with
deliberately injected missingness, so the imputation step in the training
pipeline is load-bearing rather than decorative.

Usage:
    python ml/generate_dataset.py --rows 25000 --seed 42 --output data/loans.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "credit_score",
    "debt_to_income",
    "payment_history",
    "loan_amount",
    "employment_duration",
]
TARGET = "defaulted"

# Weights on the *standardized* features for the latent default-risk score.
# Negative weight = protective (higher value lowers default risk).
_WEIGHTS = {
    "credit_score": -1.1,
    "debt_to_income": 1.3,
    "payment_history": 0.9,
    "loan_amount": 0.4,
    "employment_duration": -0.7,
}
_NOISE_SIGMA = 0.85


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _generate_features(rng: np.random.Generator, n: int) -> pd.DataFrame:
    credit_score = np.clip(rng.normal(680, 90, n).round(), 300, 850)

    debt_to_income = rng.beta(2, 5, n) * 0.65

    # Zero-inflated Poisson: an extra hard-zero mass on top of the Poisson's
    # own zero probability, representing a large "clean payer" cohort.
    hard_zero = rng.random(n) < 0.5
    poisson_counts = rng.poisson(1.2, n)
    payment_history = np.where(hard_zero, 0, poisson_counts)

    loan_amount = np.clip(rng.lognormal(mean=10.8, sigma=0.65, size=n), 5_000, 500_000)

    employment_duration = np.clip(rng.exponential(scale=6.0, size=n), 0, 40)

    return pd.DataFrame(
        {
            "credit_score": credit_score,
            "debt_to_income": debt_to_income,
            "payment_history": payment_history.astype(float),
            "loan_amount": loan_amount,
            "employment_duration": employment_duration,
        }
    )


def _standardize(series: pd.Series) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    std = values.std()
    return (values - values.mean()) / std if std > 0 else np.zeros_like(values)


def _calibrate_bias(score: np.ndarray, target_rate: float) -> float:
    """Binary-search a bias term so mean(sigmoid(score + bias)) ~= target_rate."""
    lo, hi = -10.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        rate = _sigmoid(score + mid).mean()
        if rate < target_rate:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def generate_dataset(n_rows: int, seed: int, target_default_rate: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = _generate_features(rng, n_rows)

    # Latent score computed on standardized (complete, pre-missingness) values
    # so the label reflects the true underlying risk, not reporting gaps.
    latent = np.zeros(n_rows)
    for feature, weight in _WEIGHTS.items():
        latent += weight * _standardize(df[feature])
    latent += rng.normal(0, _NOISE_SIGMA, n_rows)

    bias = _calibrate_bias(latent, target_default_rate)
    default_probability = _sigmoid(latent + bias)
    df[TARGET] = rng.binomial(1, default_probability)

    # Inject missing-at-random gaps *after* label generation.
    dti_missing = rng.random(n_rows) < 0.04
    emp_missing = rng.random(n_rows) < 0.06
    df.loc[dti_missing, "debt_to_income"] = np.nan
    df.loc[emp_missing, "employment_duration"] = np.nan

    df.insert(0, "id", np.arange(1, n_rows + 1))
    return df


def _print_summary(df: pd.DataFrame) -> None:
    positive_rate = df[TARGET].mean()
    print(f"rows: {len(df)}")
    print(f"default rate: {positive_rate:.4f} ({df[TARGET].sum()} positive / {len(df)} total)")
    print("missingness:")
    for col in ("debt_to_income", "employment_duration"):
        pct = df[col].isna().mean()
        print(f"  {col}: {pct:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=25_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--default-rate", type=float, default=0.08)
    parser.add_argument("--output", type=Path, default=Path("data/loans.csv"))
    args = parser.parse_args()

    df = generate_dataset(args.rows, args.seed, args.default_rate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)

    _print_summary(df)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
