from __future__ import annotations

from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import ObjectivesSettings


class _FakeSettingsManager:
    def __init__(self) -> None:
        self.settings = Settings()
        self.saved_count = 0
        self.replaced: list[Settings] = []

    def replace(self, settings: Settings) -> None:
        self.settings = settings
        self.replaced.append(settings)

    def save(self) -> None:
        self.saved_count += 1

    def replace_and_save(
        self,
        settings: Settings,
        *,
        preserve_exposure_policy: bool = False,
    ) -> None:
        updated = settings.clone()
        if preserve_exposure_policy:
            updated.exposure_policy = self.settings.exposure_policy.clone()
        self.replace(updated)
        self.save()

    def objectives_configuration(self) -> ObjectivesSettings:
        return self.settings.objectives


class _CalibrationTokenStage:
    def __init__(self, current_token: object, *, busy: bool = True) -> None:
        self.current_token = current_token
        self.busy = busy
        self.applied: list[object] = []
        self.accepted_candidates: list[tuple[object, object]] = []
        self.published_candidates: list[object] = []
        self.rejected_candidates: list[object] = []

    def is_busy(self) -> bool:
        return self.busy

    def is_calibration_task_token_current(self, token: object) -> bool:
        return token is self.current_token

    def apply_objective_configuration(self, objective, _objectives) -> None:
        self.applied.append(objective)

    def accept_objective_calibration_candidate(
        self,
        token: object,
        matrix: object,
    ) -> bool:
        if token is not self.current_token:
            return False
        self.accepted_candidates.append((token, matrix))
        return True

    def publish_objective_calibration_candidate(self, token: object) -> bool:
        if token is not self.current_token:
            return False
        self.published_candidates.append(token)
        self.runtime_matrix = self.accepted_candidates[-1][1]
        return True

    def reject_objective_calibration_candidate(self, token: object) -> bool:
        self.rejected_candidates.append(token)
        return True
