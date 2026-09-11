"""Labeled evaluation datasets and calibration metrics for CitePrism."""

from src.evaluation.dataset import (
    DATASET_SCHEMA_VERSION,
    EvaluationDataset,
    LabeledItem,
    load_dataset,
    write_dataset,
)
from src.evaluation.metrics import (
    brier_score,
    calibration_bins,
    classification_report,
    expected_calibration_error,
)
from src.evaluation.runner import evaluate_audit, evaluate_dataset

__all__ = [
    "DATASET_SCHEMA_VERSION",
    "EvaluationDataset",
    "LabeledItem",
    "brier_score",
    "calibration_bins",
    "classification_report",
    "evaluate_audit",
    "evaluate_dataset",
    "expected_calibration_error",
    "load_dataset",
    "write_dataset",
]
