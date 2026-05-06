#!/usr/bin/env python3
"""Tests for scene_scheduler.py — no HA needed, pure logic."""

import datetime
import pytest
from scene_scheduler import (
    SceneScheduler,
    DEFAULT_SCENES,
    approximate_sun_times,
    voice_to_scene,
    _time_to_minutes,
)


# Fixed sun times for deterministic tests
FIXED_SUN = {"sunrise": "06:30", "sunset": "18:30"}


class TestSunTimes:
    """Validate approximate sunrise/sunset for Malta."""

    def test_march_equinox(self):
        result = approximate_sun_times(datetime.date(2026, 3, 20))
        # Malta sunrise ~6:15-7:15 (CET in early March), sunset ~18:15-18:45
        sunrise_min = _time_to_minutes(result["sunrise"])
        sunset_min = _time_to_minutes(result["sunset"])
        assert 360 <= sunrise_min <= 435, f"sunrise {result['sunrise']} out of range"
        assert 1080 <= sunset_min <= 1170, f"sunset {result['sunset']} out of range"

    def test_june_solstice(self):
        result = approximate_sun_times(datetime.date(2026, 6, 21))
        sunrise_min = _time_to_minutes(result["sunrise"])
        sunset_min = _time_to_minutes(result["sunset"])
        # Longest day: sunrise ~5:45, sunset ~20:15
        assert 330 <= sunrise_min <= 390, f"sunrise {result['sunrise']} out of range"
        assert 1170 <= sunset_min <= 1230, f"sunset {result['sunset']} out of range"

    def test_december_solstice(self):
        result = approximate_sun_times(datetime.date(2026, 12, 21))
        sunrise_min = _time_to_minutes(result["sunrise"])
        sunset_min = _time_to_minutes(result["sunset"])
        # Shortest day: sunrise ~7:00, sunset ~16:45
        assert 390 <= sunrise_min <= 450, f"sunrise {result['sunrise']} out of range"
        assert 975 <= sunset_min <= 1035, f"sunset {result['sunset']} out of range"


class TestEvaluate:
    """Scene evaluation based on time, sun, occupancy."""

    def setup_method(self):
        self.scheduler = SceneScheduler(sun_times=FIXED_SUN)

    def test_early_morning_activates_morning(self):
        # 07:00 — after sunrise, within morning window
        active = self.scheduler.evaluate(now_minutes=420, is_home=True, sun_times=FIXED_SUN)
        assert "morning" in active

    def test_midday_activates_daytime(self):
        active = self.scheduler.evaluate(now_minutes=720, is_home=True, sun_times=FIXED_SUN)
        assert "daytime" in active

    def test_evening_activates_evening(self):
        # 17:00 (1020 min) — within evening window, before sunset (18:30) - 60min offset
        active = self.scheduler.evaluate(now_minutes=1020, is_home=True, sun_times=FIXED_SUN)
        assert "evening" in active

    def test_night_activates_night(self):
        # 23:00 — within night window
        active = self.scheduler.evaluate(now_minutes=1380, is_home=True, sun_times=FIXED_SUN)
        assert "night" in active

    def test_overnight_activates_night(self):
        # 03:00 — within overnight window (22:30 → 06:30)
        active = self.scheduler.evaluate(now_minutes=180, is_home=True, sun_times=FIXED_SUN)
        assert "night" in active

    def test_not_home_activates_away(self):
        active = self.scheduler.evaluate(now_minutes=720, is_home=False, sun_times=FIXED_SUN)
        assert "away" in active
        assert "daytime" not in active  # requires_home=True

    def test_not_home_skips_home_scenes(self):
        active = self.scheduler.evaluate(now_minutes=420, is_home=False, sun_times=FIXED_SUN)
        assert "morning" not in active
        assert "away" in active

    def test_before_sunrise_no_morning(self):
        # 06:00 — before sunrise, even though after 06:30 doesn't matter yet
        # Actually 06:00 < 06:30 so "after" check fails first
        active = self.scheduler.evaluate(now_minutes=360, is_home=True, sun_times=FIXED_SUN)
        assert "morning" not in active


class TestActivate:
    """Scene activation and override logic."""

    def setup_method(self):
        self.scheduler = SceneScheduler(sun_times=FIXED_SUN)

    def test_activate_returns_actions(self):
        result = self.scheduler.activate("evening")
        assert result["scene"] == "evening"
        assert len(result["actions"]) > 0
        assert result["source"] == "auto"

    def test_activate_unknown_scene(self):
        result = self.scheduler.activate("nonexistent")
        assert result["error"] is not None

    def test_voice_override_with_duration(self):
        self.scheduler.activate("night", source="voice", duration_min=30)
        status = self.scheduler.status()
        assert status["override"] == "night"
        assert status["override_until"] is not None

    def test_auto_activate_no_override(self):
        self.scheduler.activate("daytime", source="auto")
        status = self.scheduler.status()
        assert status["override"] is None

    def test_activation_log(self):
        self.scheduler.activate("morning")
        self.scheduler.activate("evening")
        status = self.scheduler.status()
        assert status["activation_count"] == 2
        assert len(status["recent_activations"]) == 2


class TestVoiceTrigger:
    """Voice transcript → scene mapping."""

    def test_good_morning(self):
        assert voice_to_scene("good morning") == "morning"

    def test_goodnight(self):
        assert voice_to_scene("goodnight") == "night"
        assert voice_to_scene("good night") == "night"

    def test_leaving(self):
        assert voice_to_scene("I'm leaving now") == "away"

    def test_heading_out(self):
        assert voice_to_scene("heading out for the day") == "away"

    def test_coming_home_clears(self):
        # "I'm home" → OVERRIDE_CLEAR sentinel (not None — distinguishes from "no match")
        from brain.scene_scheduler import OVERRIDE_CLEAR
        assert voice_to_scene("I'm home") == OVERRIDE_CLEAR
        assert voice_to_scene("i'm back") == OVERRIDE_CLEAR
        assert voice_to_scene("welcome home") == OVERRIDE_CLEAR

    def test_unknown_returns_none(self):
        assert voice_to_scene("play some music") is None


class TestGetActions:
    """Retrieving service call actions for a scene."""

    def setup_method(self):
        self.scheduler = SceneScheduler(sun_times=FIXED_SUN)

    def test_morning_actions(self):
        actions = self.scheduler.get_actions_for_scene("morning")
        assert len(actions) >= 2
        # First action should be a light
        assert actions[0]["domain"] == "light"

    def test_away_actions(self):
        actions = self.scheduler.get_actions_for_scene("away")
        assert len(actions) >= 2

    def test_unknown_scene_empty(self):
        assert self.scheduler.get_actions_for_scene("nonexistent") == []


class TestStatus:
    """Scheduler status reporting."""

    def setup_method(self):
        self.scheduler = SceneScheduler(sun_times=FIXED_SUN)

    def test_initial_status(self):
        status = self.scheduler.status()
        assert status["last_activated"] is None
        assert status["override"] is None
        assert status["activation_count"] == 0

    def test_status_after_activation(self):
        self.scheduler.activate("morning")
        status = self.scheduler.status()
        assert status["last_activated"] == "morning"
        assert status["activation_count"] == 1


class TestEdgeCases:
    """Boundary conditions and edge cases."""

    def setup_method(self):
        self.scheduler = SceneScheduler(sun_times=FIXED_SUN)

    def test_exact_boundary_time(self):
        # Exactly at "after" time should activate
        active = self.scheduler.evaluate(now_minutes=390, is_home=True, sun_times=FIXED_SUN)
        # 390 = 06:30 — exactly at morning start
        assert "morning" in active

    def test_one_minute_before_window(self):
        # 06:29 — one minute before morning
        active = self.scheduler.evaluate(now_minutes=389, is_home=True, sun_times=FIXED_SUN)
        assert "morning" not in active

    def test_empty_scenes_dict(self):
        scheduler = SceneScheduler(scenes={}, sun_times=FIXED_SUN)
        # Even with no scenes, evaluate should not crash; it returns nothing if scenes dict is truly empty
        # Note: SceneScheduler({}) actually gets DEFAULT_SCENES due to .copy() on None
        # To truly get empty, we need to pass scenes=None and override differently
        # Instead, test that custom empty works:
        active = scheduler.evaluate(now_minutes=720, is_home=True)
        assert len(active) == 0  # no scenes = no matches

    def test_custom_scene(self):
        scenes = {
            "custom": {
                "trigger": {"after": "12:00", "before": "14:00"},
                "requires_home": True,
                "actions": [{"domain": "light", "service": "turn_on", "entity_id": "light.dining"}],
                "priority": "high",
            }
        }
        scheduler = SceneScheduler(scenes=scenes, sun_times=FIXED_SUN)
        active = scheduler.evaluate(now_minutes=780, is_home=True, sun_times=FIXED_SUN)
        assert "custom" in active

    def test_sun_offset_before_sunset(self):
        # evening scene has sun: before_sunset with offset -60
        # sunset at 18:30, so before_sunset -60min = before 17:30
        # At 17:00 (1020 min), should be active
        active = self.scheduler.evaluate(now_minutes=1020, is_home=True, sun_times=FIXED_SUN)
        assert "evening" in active

    def test_log_capped_at_100(self):
        for i in range(150):
            self.scheduler.activate("morning")
        status = self.scheduler.status()
        # Log is capped at 100 entries
        assert status["activation_count"] == 100
        assert len(status["recent_activations"]) == 5  # only last 5 shown
