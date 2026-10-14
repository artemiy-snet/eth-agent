"""Ознаки ринку (Біблія, розділ 4). Лише закриті свічки: ознака свічки з open_time T
доступна з моменту T + tf, не раніше (інакше бектест «бачить майбутнє»).

compute_features (pandas) імпортується ліниво, щоб живий агент на телефоні не тягнув pandas."""

from agent.features.params import FeatureParams

__all__ = ["FeatureParams", "compute_features"]


def __getattr__(name):
    if name == "compute_features":
        from agent.features.core import compute_features
        return compute_features
    raise AttributeError(name)
