"""ModeRegistry/ModeProfile wired to the byte-preserved prompt instructions."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.prompts import MODE_PROFILES
from app.schemas.mode import AnalysisMode


@dataclass(frozen=True)
class ModeProfile:
    mode: AnalysisMode
    display_name: str
    plan_instruction: str
    execute_instruction: str
    critic_instruction: str
    required_section_keys: tuple[str, ...]


class ModeRegistry:
    def __init__(self) -> None:
        self.profiles = {
            mode: ModeProfile(
                mode=mode,
                display_name=MODE_PROFILES[mode.value].display_name,
                plan_instruction=MODE_PROFILES[mode.value].plan_instruction,
                execute_instruction=MODE_PROFILES[mode.value].execute_instruction,
                critic_instruction=MODE_PROFILES[mode.value].critic_instruction,
                required_section_keys=tuple(dict.fromkeys(MODE_PROFILES[mode.value].required_section_keys)),
            )
            for mode in AnalysisMode
            if mode.value in MODE_PROFILES
        }
        missing = set(AnalysisMode) - set(self.profiles)
        if missing:
            raise RuntimeError("AnalysisMode 未注册 ModeProfile: " + ", ".join(sorted(mode.value for mode in missing)))

    def of(self, mode: AnalysisMode | None) -> ModeProfile:
        return self.profiles.get(mode, self.profiles[AnalysisMode.GENERAL])
