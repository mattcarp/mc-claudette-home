#!/usr/bin/env python3
"""
Claudette Home — Scene Scheduler

Time-based scene activation with dawn/dusk awareness and occupancy detection.
No hardcoded schedules — scenes are defined as config dicts and evaluated every
tick against current conditions (time, sun, occupancy, manual overrides).

Integrates with:
  - ha_bridge.py → call_service to activate lights/media/climate
  - intent_parser.py → "good morning" / "I'm leaving" voice triggers
  - proactive_alerts.py → schedule-aware alert suppression

Scenes are NOT automations. They describe desired state; the scheduler reconciles
current state → desired state each tick and emits service calls for the delta.

Part of EPIC 2 (#2) — Whole-home orchestration.
"""

import json
import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Scene definitions — purely declarative, no logic
# ---------------------------------------------------------------------------

DEFAULT_SCENES = {
    "morning": {
        "trigger": {"after": "06:30", "before": "09:00", "sun": "after_sunrise"},
        "requires_home": True,
        "actions": [
            {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen", "params": {"brightness_pct": 80, "color_temp": 350}},
            {"domain": "light", "service": "turn_on", "entity_id": "light.living_room", "params": {"brightness_pct": 60, "color_temp": 400}},
            {"domain": "media_player", "service": "play_media", "entity_id": "media_player.wiim", "params": {"media_content_id": "spotify:playlist:morning", "media_content_type": "music"}},
        ],
        "priority": "medium",
    },
    "daytime": {
        "trigger": {"after": "09:00", "before": "17:00"},
        "requires_home": True,
        "actions": [
            {"domain": "light", "service": "turn_off", "entity_id": "light.all"},
        ],
        "priority": "low",
    },
    "evening": {
        "trigger": {"after": "17:00", "before": "22:30", "sun": "before_sunset", "sun_offset_min": -60},
        "requires_home": True,
        "actions": [
            {"domain": "light", "service": "turn_on", "entity_id": "light.living_room", "params": {"brightness_pct": 70, "color_temp": 2700}},
            {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen", "params": {"brightness_pct": 50, "color_temp": 2700}},
        ],
        "priority": "medium",
    },
    "night": {
        "trigger": {"after": "22:30", "before": "06:30"},
        "requires_home": True,
        "actions": [
            {"domain": "light", "service": "turn_off", "entity_id": "light.all"},
            {"domain": "light", "service": "turn_on", "entity_id": "light.hallway", "params": {"brightness_pct": 10, "color_temp": 2200}},
        ],
        "priority": "medium",
    },
    "away": {
        "trigger": {},  # Manual/voice trigger only
        "requires_home": False,
        "actions": [
            {"domain": "light", "service": "turn_off", "entity_id": "light.all"},
            {"domain": "climate", "service": "set_hvac_mode", "entity_id": "climate.home", "params": {"hvac_mode": "off"}},
            {"domain": "media_player", "service": "turn_off", "entity_id": "media_player.wiim"},
        ],
        "priority": "high",
    },
}


# ---------------------------------------------------------------------------
# Sun position helpers (no API — pure calculation for Malta lat/lon)
# ---------------------------------------------------------------------------

MALTA_LAT = 35.94
MALTA_LON = 14.38

def _to_rad(deg: float) -> float:
    return deg * 3.141592653589793 / 180.0

def approximate_sun_times(date: datetime.date, lat: float = MALTA_LAT, lon: float = MALTA_LON) -> dict:
    """Approximate sunrise/sunset using the NOAA solar calculator.
    
    Accuracy: ±5 min for Malta. Good enough for scene scheduling.
    Returns: {"sunrise": HH:MM, "sunset": HH:MM} in local time.
    """
    import math
    
    day_of_year = date.timetuple().tm_yday
    
    # Solar declination
    decl_deg = 23.45 * math.sin(math.radians(360 / 365 * (day_of_year - 81)))
    decl_rad = math.radians(decl_deg)
    
    lat_rad = math.radians(lat)
    
    # Hour angle: cos(ha) = (sin(-0.833°) - sin(lat)*sin(decl)) / (cos(lat)*cos(decl))
    # -0.833° accounts for solar zenith with atmospheric refraction
    cos_ha = (math.sin(math.radians(-0.833)) - math.sin(lat_rad) * math.sin(decl_rad)) / (
        math.cos(lat_rad) * math.cos(decl_rad)
    )
    
    cos_ha = max(-1, min(1, cos_ha))  # clamp for polar edge cases
    hour_angle = math.degrees(math.acos(cos_ha))
    
    # Equation of time (minutes)
    B = math.radians(360 / 365 * (day_of_year - 81))
    EoT = 229.18 * (0.000075 + 0.001868 * math.cos(B) - 0.032077 * math.sin(B)
                     - 0.014615 * math.cos(2 * B) - 0.040849 * math.sin(2 * B))
    
    # Solar noon (UTC hours)
    solar_noon_utc = 12 - lon / 15 - EoT / 60
    
    sunrise_utc = solar_noon_utc - hour_angle / 15
    sunset_utc = solar_noon_utc + hour_angle / 15
    
    # Malta: CET (UTC+1) Oct-Mar, CEST (UTC+2) Mar-Oct
    utc_offset = 2 if 3 <= date.month <= 10 else 1
    
    sunrise_local = sunrise_utc + utc_offset
    sunset_local = sunset_utc + utc_offset
    
    def _fmt_hours(h):
        h = h % 24
        hh = int(h)
        mm = int((h - hh) * 60)
        return f"{hh:02d}:{mm:02d}"
    
    return {"sunrise": _fmt_hours(sunrise_local), "sunset": _fmt_hours(sunset_local)}


def _time_to_minutes(t: str) -> int:
    """Convert 'HH:MM' to minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _now_minutes() -> int:
    """Current local time in minutes since midnight."""
    now = datetime.datetime.now()
    return now.hour * 60 + now.minute


# ---------------------------------------------------------------------------
# Scene Scheduler
# ---------------------------------------------------------------------------

class SceneScheduler:
    """Evaluates which scenes should be active based on current conditions."""
    
    def __init__(self, scenes: Optional[dict] = None, sun_times: Optional[dict] = None):
        self.scenes = scenes if scenes is not None else DEFAULT_SCENES.copy()
        self._manual_override: Optional[str] = None
        self._override_until: Optional[datetime.datetime] = None
        self._last_activated: Optional[str] = None
        self._activation_log: list = []
        self._sun_times = sun_times  # injectable for testing
    
    def get_sun_times(self, date: Optional[datetime.date] = None) -> dict:
        """Get sunrise/sunset, using cached or computed values."""
        if self._sun_times:
            return self._sun_times
        return approximate_sun_times(date or datetime.date.today())
    
    def evaluate(
        self,
        now_minutes: Optional[int] = None,
        is_home: bool = True,
        sun_times: Optional[dict] = None,
    ) -> list:
        """Return list of scene names that should be active right now.
        
        Args:
            now_minutes: current time in minutes since midnight (auto if None)
            is_home: whether anyone is home
            sun_times: {"sunrise": "HH:MM", "sunset": "HH:MM"} (auto if None)
        
        Returns:
            List of active scene names, ordered by priority (high first).
        """
        if now_minutes is None:
            now_minutes = _now_minutes()
        if sun_times is None:
            sun_times = self.get_sun_times()
        
        # Check manual override
        if self._manual_override and self._override_until:
            if datetime.datetime.now() < self._override_until:
                return [self._manual_override]
            else:
                self._clear_override()
        
        active = []
        for name, scene in self.scenes.items():
            trigger = scene.get("trigger", {})
            
            # Check occupancy requirement
            if scene.get("requires_home", True) and not is_home:
                continue
            
            # Empty trigger = manual-only scene (e.g., "away")
            if not trigger:
                if not is_home:
                    active.append(name)
                continue
            
            # Check time window
            after = trigger.get("after")
            before = trigger.get("before")
            
            if after and before:
                after_min = _time_to_minutes(after)
                before_min = _time_to_minutes(before)
                # Handle overnight (e.g., 22:30 → 06:30)
                if after_min > before_min:
                    if not (now_minutes >= after_min or now_minutes < before_min):
                        continue
                else:
                    if not (after_min <= now_minutes < before_min):
                        continue
            elif after:
                if now_minutes < _time_to_minutes(after):
                    continue
            elif before:
                if now_minutes >= _time_to_minutes(before):
                    continue
            
            # Check sun condition
            sun_condition = trigger.get("sun")
            sun_offset = trigger.get("sun_offset_min", 0)
            if sun_condition:
                sunrise_min = _time_to_minutes(sun_times.get("sunrise", "06:30"))
                sunset_min = _time_to_minutes(sun_times.get("sunset", "18:30"))
                
                if sun_condition == "after_sunrise":
                    if now_minutes < sunrise_min + sun_offset:
                        continue
                elif sun_condition == "before_sunset":
                    if now_minutes > sunset_min + sun_offset:
                        continue
                elif sun_condition == "after_sunset":
                    if now_minutes < sunset_min + sun_offset:
                        continue
                elif sun_condition == "before_sunrise":
                    if now_minutes > sunrise_min + sun_offset:
                        continue
            
            active.append(name)
        
        # Sort by priority (high > medium > low)
        priority_order = {"high": 0, "medium": 1, "low": 2}
        active.sort(key=lambda n: priority_order.get(self.scenes[n].get("priority", "low"), 3))
        
        return active
    
    def activate(self, scene_name: str, source: str = "auto", duration_min: Optional[int] = None) -> dict:
        """Activate a scene, optionally with a time-limited override.
        
        Returns:
            {"scene": name, "actions": [...], "source": source}
        """
        if scene_name not in self.scenes:
            return {"scene": None, "actions": [], "source": source, "error": f"unknown scene: {scene_name}"}
        
        scene = self.scenes[scene_name]
        result = {
            "scene": scene_name,
            "actions": scene.get("actions", []),
            "source": source,
        }
        
        self._last_activated = scene_name
        entry = {
            "scene": scene_name,
            "source": source,
            "timestamp": datetime.datetime.now().isoformat(),
            "actions_count": len(scene.get("actions", [])),
        }
        self._activation_log.append(entry)
        if len(self._activation_log) > 100:
            self._activation_log = self._activation_log[-100:]
        
        if duration_min and source == "voice":
            self._manual_override = scene_name
            self._override_until = datetime.datetime.now() + datetime.timedelta(minutes=duration_min)
        
        return result
    
    def _clear_override(self):
        self._manual_override = None
        self._override_until = None
    
    def get_active_scene(self, **kwargs) -> Optional[str]:
        """Convenience: return the single best active scene name."""
        active = self.evaluate(**kwargs)
        return active[0] if active else None
    
    def status(self) -> dict:
        """Return scheduler status for dashboards."""
        return {
            "last_activated": self._last_activated,
            "override": self._manual_override,
            "override_until": self._override_until.isoformat() if self._override_until else None,
            "activation_count": len(self._activation_log),
            "recent_activations": self._activation_log[-5:],
        }
    
    def get_actions_for_scene(self, scene_name: str) -> list:
        """Get the service call actions for a scene."""
        if scene_name not in self.scenes:
            return []
        return self.scenes[scene_name].get("actions", [])


# ---------------------------------------------------------------------------
# Voice trigger mapping — intent parser integration point
# ---------------------------------------------------------------------------

VOICE_TRIGGER_MAP = {
    "good morning": "morning",
    "morning": "morning",
    "goodnight": "night",
    "good night": "night",
    "i'm leaving": "away",
    "heading out": "away",
    "leaving": "away",
    "i'm home": None,  # clear override, let auto schedule resume
    "i'm back": None,
}


def voice_to_scene(transcript: str) -> Optional[str]:
    """Map a voice transcript to a scene name.
    
    Returns None if no match (intent parser handles unknowns).
    """
    lower = transcript.lower().strip()
    for trigger, scene in VOICE_TRIGGER_MAP.items():
        if trigger in lower:
            return scene
    return None
