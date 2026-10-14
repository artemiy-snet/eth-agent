"""Ознаки ринку (Біблія, розділ 4). Лише закриті свічки: ознака свічки з open_time T
доступна з моменту T + tf, не раніше (інакше бектест «бачить майбутнє»)."""

from agent.features.core import FeatureParams, compute_features

__all__ = ["FeatureParams", "compute_features"]
