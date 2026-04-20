#!/usr/bin/env python3
"""
Claudette Home — Whole-Home Audio Controller
Controls WiiM streamer + Echo Dots via Home Assistant as a unified audio system.

Capabilities:
  - Multi-zone playback (whole house, downstairs, bedrooms, specific rooms)
  - Volume control per zone or globally
  - Doorbell duck-and-announce with auto-restore
  - Claudette TTS announcement on all/specific speakers
  - Music control (play, pause, stop, skip, source)
  - Volume snapshot/restore (for ducking around announcements)
  - Entity auto-discovery from HA (--action sync)

Usage (standalone):
  python3 whole_home_audio.py --action announce --message "Dinner is ready"
  python3 whole_home_audio.py --action play --zone whole_house --source spotify
  python3 whole_home_audio.py --action volume --zone whole_house --level 0.4
  python3 whole_home_audio.py --action doorbell --message "Someone at the front door"
  python3 whole_home_audio.py --action status
  python3 whole_home_audio.py --action sync      # discover entities from HA
  python3 whole_home_audio.py --stub --action announce --message "Test"

As a module (from intent parser response):
  from whole_home_audio import AudioController
  ctrl = AudioController()
  ctrl.sync_entities()          # discover actual entity IDs from HA
  ctrl.announce("Dinner is ready", zone="whole_house")
  ctrl.set_volume("whole_house", 0.5)
  ctrl.doorbell_announce("Front door")

Environment:
  HA_URL            — Home Assistant base URL (default: http://localhost:8123)
  HA_TOKEN          — Long-lived access token (required unless --stub)
  AUDIO_DUCK_LEVEL  — Volume level during announcements (default: 0.15)
  AUDIO_RESTORE_LEVEL — Volume level after announcements (default: 0.4)

Zone → Entity Mapping (update entity IDs after running --action sync):
  whole_house  → media_player.whole_house (HA group)
  downstairs   → media_player.downstairs
  bedrooms     → media_player.bedrooms
  living_room  → media_player.echo_dot_living_room
  kitchen      → media_player.echo_dot_kitchen
  bedroom      → media_player.echo_dot_bedroom
  wiim         → media_player.wiim_mini

Run --action sync to auto-discover entities from HA and print updated zone mapping.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
DUCK_LEVEL = float(os.environ.get("AUDIO_DUCK_LEVEL", "0.15"))
RESTORE_LEVEL = float(os.environ.get("AUDIO_RESTORE_LEVEL", "0.4"))

# Zone name → HA entity ID mapping
ZONE_ENTITIES: Dict[str, str] = {
    "whole_house": "media_player.whole_house",
    "whole house": "media_player.whole_house",
    "everywhere": "media_player.whole_house",
    "all": "media_player.whole_house",
    "downstairs": "media_player.downstairs",
    "bedrooms": "media_player.bedrooms",
    "living_room": "media_player.echo_dot_living_room",
    "living room": "media_player.echo_dot_living_room",
    "kitchen": "media_player.echo_dot_kitchen",
    "bedroom": "media_player.echo_dot_bedroom",
    "master bedroom": "media_player.echo_dot_bedroom",
    "bathroom": "media_player.echo_dot_bathroom",
    "office": "media_player.echo_dot_office",
    "hallway": "media_player.echo_dot_hallway",
    "wiim": "media_player.wiim_mini",
    "hi-fi": "media_player.wiim_mini",
    "hifi": "media_player.wiim_mini",
}

# Doorbell entity
DOORBELL_ENTITY = os.environ.get(
    "HA_DOORBELL_ENTITY", "binary_sensor.eufy_doorbell_t8200_button"
)

# Alexa announce service prefix
ALEXA_NOTIFY_PREFIX = "notify.alexa_media"

# TTS service to use (alexa_announce > google_translate_say for Echo Dots)
TTS_MODE = os.environ.get("AUDIO_TTS_MODE", "alexa_announce")  # or "google_tts"


class AudioError(Exception):
    """Raised when HA audio service call fails."""
    pass


# ---------------------------------------------------------------------------
# Stub for dev/test (no HA required)
# ---------------------------------------------------------------------------
class AudioControllerStub:
    """
    In-memory stub for testing without Home Assistant.
    Tracks calls so tests can assert behaviour.
    """

    def __init__(self):
        self.calls: List[Dict] = []
        self._volumes: Dict[str, float] = {}
        self._playing: Dict[str, bool] = {}

    def _log(self, action: str, **kwargs):
        entry = {"action": action, **kwargs}
        self.calls.append(entry)
        logger.info("[STUB] %s", json.dumps(entry))
        return entry

    def announce(self, message: str, zone: str = "whole_house") -> Dict:
        return self._log("announce", message=message, zone=zone)

    def doorbell_announce(
        self,
        message: str = "Someone is at the front door.",
        duck_level: float = DUCK_LEVEL,
        restore_level: float = RESTORE_LEVEL,
        pause_seconds: float = 4.0,
    ) -> Dict:
        self._volumes["whole_house_pre_duck"] = self._volumes.get("whole_house", 0.4)
        self._volumes["whole_house"] = duck_level
        result = self._log(
            "doorbell_announce",
            message=message,
            duck_level=duck_level,
            restore_level=restore_level,
        )
        self._volumes["whole_house"] = restore_level
        return result

    def set_volume(self, zone: str, level: float) -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._volumes[entity] = level
        return self._log("set_volume", zone=zone, entity=entity, level=level)

    def volume_up(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        current = self._volumes.get(entity, RESTORE_LEVEL)
        next_level = min(1.0, round(current + 0.1, 2))
        self._volumes[entity] = next_level
        return self._log("volume_up", zone=zone, entity=entity, level=next_level)

    def volume_down(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        current = self._volumes.get(entity, RESTORE_LEVEL)
        next_level = max(0.0, round(current - 0.1, 2))
        self._volumes[entity] = next_level
        return self._log("volume_down", zone=zone, entity=entity, level=next_level)

    def play(self, zone: str = "whole_house", source: Optional[str] = None, content_id: Optional[str] = None) -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._playing[entity] = True
        return self._log("play", zone=zone, entity=entity, source=source, content_id=content_id)

    def pause(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._playing[entity] = False
        return self._log("pause", zone=zone, entity=entity)

    def stop(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._playing[entity] = False
        return self._log("stop", zone=zone, entity=entity)

    def next_track(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._playing[entity] = True
        return self._log("next_track", zone=zone, entity=entity)

    def previous_track(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        self._playing[entity] = True
        return self._log("previous_track", zone=zone, entity=entity)

    def status(self, zone: str = "whole_house") -> Dict:
        entity = ZONE_ENTITIES.get(zone, zone)
        return {
            "entity": entity,
            "state": "playing" if self._playing.get(entity) else "idle",
            "volume": self._volumes.get(entity, 0.4),
            "stub": True,
        }

    def execute_intent(self, intent: Dict) -> Dict:
        """Route intent parser output → appropriate audio method."""
        action = intent.get("action", "")
        zone = intent.get("zone", "whole_house")

        if action == "announce":
            return self.announce(intent.get("message", ""), zone=zone)
        if action == "doorbell_announce":
            return self.doorbell_announce(
                message=intent.get("message", "Someone is at the front door.")
            )
        if action == "play":
            return self.play(
                zone=zone,
                source=intent.get("source"),
                content_id=intent.get("content_id"),
            )
        if action == "pause":
            return self.pause(zone)
        if action == "stop":
            return self.stop(zone)
        if action == "next_track":
            return self.next_track(zone)
        if action == "previous_track":
            return self.previous_track(zone)
        if action == "volume":
            return self.set_volume(zone, intent.get("level", RESTORE_LEVEL))
        if action == "volume_up":
            return self.volume_up(zone)
        if action == "volume_down":
            return self.volume_down(zone)
        if action == "status":
            return self.status(zone)

        return self._log("execute_intent", intent=intent)


# ---------------------------------------------------------------------------
# Real controller (needs HA)
# ---------------------------------------------------------------------------
class AudioController:
    """
    Controls whole-home audio via Home Assistant REST API.
    Wraps HA service calls for WiiM + Echo Dot management.
    """

    def __init__(
        self,
        ha_url: str = HA_URL,
        ha_token: str = HA_TOKEN,
    ):
        if not ha_token:
            raise EnvironmentError(
                "HA_TOKEN not set. Generate one in HA → Profile → Long-Lived Access Tokens."
            )
        self.ha_url = ha_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }
        try:
            import requests as req
            self._requests = req
        except ImportError:
            raise ImportError("requests not installed — run: pip install requests")

    # -------------------------------------------------------------------
    # Internal HA caller
    # -------------------------------------------------------------------
    def _call_service(self, domain: str, service: str, data: Dict) -> Dict:
        url = f"{self.ha_url}/api/services/{domain}/{service}"
        resp = self._requests.post(url, headers=self.headers, json=data, timeout=10)
        if resp.status_code not in (200, 201):
            raise AudioError(
                f"HA service call failed: {domain}.{service} → {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.text else {}

    def _get_state(self, entity_id: str) -> Dict:
        url = f"{self.ha_url}/api/states/{entity_id}"
        resp = self._requests.get(url, headers=self.headers, timeout=10)
        if resp.status_code == 404:
            return {"entity_id": entity_id, "state": "unavailable"}
        resp.raise_for_status()
        return resp.json()

    def _resolve_zone(self, zone: str) -> str:
        """Convert zone name → HA entity ID."""
        return ZONE_ENTITIES.get(zone.lower(), zone)

    # -------------------------------------------------------------------
    # Announce (TTS on Echo Dots)
    # -------------------------------------------------------------------
    def announce(self, message: str, zone: str = "whole_house") -> Dict:
        """
        Speak a message on Echo Dots in the given zone.
        Uses Alexa Media Player 'announce' type so it interrupts music cleanly.
        Falls back to google_translate_say if Alexa integration not available.
        """
        entity = self._resolve_zone(zone)
        logger.info("Announcing '%s' on %s", message, entity)

        if TTS_MODE == "alexa_announce":
            # Alexa Media Player notify service — proper announce that interrupts music
            # Service name pattern: notify.alexa_media_<entity_suffix>
            entity_suffix = entity.replace("media_player.", "").replace(".", "_")
            notify_service = f"{ALEXA_NOTIFY_PREFIX}_{entity_suffix}"
            data = {
                "message": message,
                "data": {"type": "announce"},
            }
            try:
                return self._call_service("notify", notify_service, data)
            except AudioError:
                logger.warning(
                    "Alexa notify service %s not found, falling back to google TTS",
                    notify_service,
                )
                # Fall through to google TTS

        # Fallback: google_translate_say (works without Alexa Media Player)
        return self._call_service(
            "tts",
            "google_translate_say",
            {"entity_id": entity, "message": message, "language": "en"},
        )

    # -------------------------------------------------------------------
    # Doorbell duck-and-announce
    # -------------------------------------------------------------------
    def doorbell_announce(
        self,
        message: str = "Someone is at the front door.",
        duck_level: float = DUCK_LEVEL,
        restore_level: float = RESTORE_LEVEL,
        pause_seconds: float = 4.0,
    ) -> Dict:
        """
        Doorbell announcement pattern:
          1. Duck whole-house volume
          2. Announce on all speakers
          3. Wait for TTS to finish
          4. Restore volume
        """
        logger.info("Doorbell: duck → announce → restore")
        entity = ZONE_ENTITIES["whole_house"]

        # 1. Duck volume
        self._call_service(
            "media_player",
            "volume_set",
            {"entity_id": entity, "volume_level": duck_level},
        )
        time.sleep(0.3)

        # 2. Announce
        self.announce(message, zone="whole_house")

        # 3. Wait for TTS
        time.sleep(pause_seconds)

        # 4. Restore volume
        self._call_service(
            "media_player",
            "volume_set",
            {"entity_id": entity, "volume_level": restore_level},
        )

        return {"announced": True, "message": message, "duck_level": duck_level, "restore_level": restore_level}

    # -------------------------------------------------------------------
    # Volume
    # -------------------------------------------------------------------
    def set_volume(self, zone: str, level: float) -> Dict:
        """Set volume for a zone. Level: 0.0–1.0"""
        level = max(0.0, min(1.0, level))
        entity = self._resolve_zone(zone)
        logger.info("Volume %s → %.0f%%", entity, level * 100)
        return self._call_service(
            "media_player",
            "volume_set",
            {"entity_id": entity, "volume_level": level},
        )

    def volume_up(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        return self._call_service("media_player", "volume_up", {"entity_id": entity})

    def volume_down(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        return self._call_service("media_player", "volume_down", {"entity_id": entity})

    # -------------------------------------------------------------------
    # Playback control
    # -------------------------------------------------------------------
    def play(
        self,
        zone: str = "whole_house",
        source: Optional[str] = None,
        content_id: Optional[str] = None,
        content_type: str = "music",
    ) -> Dict:
        """
        Start playback on a zone.
        If source is given (e.g. 'spotify'), switch WiiM input then play.
        If content_id is given (Spotify URI, etc), play that specific content.
        """
        entity = self._resolve_zone(zone)

        if source:
            # Switch WiiM to the given source (e.g. Spotify, AirPlay, Bluetooth)
            wiim_entity = ZONE_ENTITIES["wiim"]
            logger.info("Switching WiiM source → %s", source)
            self._call_service(
                "media_player",
                "select_source",
                {"entity_id": wiim_entity, "source": source.title()},
            )

        if content_id:
            logger.info("Playing content %s on %s", content_id, entity)
            return self._call_service(
                "media_player",
                "play_media",
                {
                    "entity_id": entity,
                    "media_content_id": content_id,
                    "media_content_type": content_type,
                },
            )

        # Resume current media
        logger.info("Play (resume) on %s", entity)
        return self._call_service("media_player", "media_play", {"entity_id": entity})

    def pause(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        logger.info("Pause %s", entity)
        return self._call_service("media_player", "media_pause", {"entity_id": entity})

    def stop(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        logger.info("Stop %s", entity)
        return self._call_service("media_player", "media_stop", {"entity_id": entity})

    def next_track(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        return self._call_service("media_player", "media_next_track", {"entity_id": entity})

    def previous_track(self, zone: str = "whole_house") -> Dict:
        entity = self._resolve_zone(zone)
        return self._call_service("media_player", "media_previous_track", {"entity_id": entity})

    # -------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------
    def status(self, zone: str = "whole_house") -> Dict:
        """Return current state of a zone's media player."""
        entity = self._resolve_zone(zone)
        state = self._get_state(entity)
        attrs = state.get("attributes", {})
        return {
            "entity": entity,
            "state": state.get("state", "unknown"),
            "volume": attrs.get("volume_level"),
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
            "source": attrs.get("source"),
            "is_playing": state.get("state") == "playing",
        }

    # -------------------------------------------------------------------
    # Intent router
    # -------------------------------------------------------------------
    def execute_intent(self, intent: Dict) -> Dict:
        """
        Route intent parser output to the appropriate audio method.
        
        Expected intent shapes:
          {"action": "call_service", "domain": "media_player", "service": "volume_set",
           "entity_id": "media_player.whole_house", "service_data": {"volume_level": 0.4}}

          {"action": "announce", "message": "...", "zone": "whole_house"}

          {"action": "doorbell_announce", "message": "..."}
        """
        action = intent.get("action", "")
        zone = intent.get("zone", "whole_house")

        if action == "announce":
            return self.announce(
                intent.get("message", ""),
                zone=zone,
            )

        if action == "doorbell_announce":
            return self.doorbell_announce(
                message=intent.get("message", "Someone is at the front door.")
            )

        if action == "play":
            return self.play(
                zone=zone,
                source=intent.get("source"),
                content_id=intent.get("content_id"),
                content_type=intent.get("content_type", "music"),
            )

        if action == "pause":
            return self.pause(zone)

        if action == "stop":
            return self.stop(zone)

        if action == "next_track":
            return self.next_track(zone)

        if action == "previous_track":
            return self.previous_track(zone)

        if action == "volume":
            return self.set_volume(zone, intent.get("level", RESTORE_LEVEL))

        if action == "volume_up":
            return self.volume_up(zone)

        if action == "volume_down":
            return self.volume_down(zone)

        if action == "status":
            return self.status(zone)

        if action == "call_service":
            domain = intent.get("domain", "")
            service = intent.get("service", "")
            service_data = intent.get("service_data", {})
            entity_id = intent.get("entity_id", "")
            if entity_id:
                service_data["entity_id"] = entity_id

            # Route common media_player intents to typed methods for better logging
            if domain == "media_player":
                if service == "volume_set":
                    zone = self._entity_to_zone(entity_id)
                    return self.set_volume(zone, service_data.get("volume_level", 0.4))
                if service == "media_play":
                    zone = self._entity_to_zone(entity_id)
                    return self.play(zone)
                if service == "media_pause":
                    zone = self._entity_to_zone(entity_id)
                    return self.pause(zone)
                if service == "media_stop":
                    zone = self._entity_to_zone(entity_id)
                    return self.stop(zone)
                if service == "media_next_track":
                    zone = self._entity_to_zone(entity_id)
                    return self.next_track(zone)
                if service == "media_previous_track":
                    zone = self._entity_to_zone(entity_id)
                    return self.previous_track(zone)
                if service == "volume_up":
                    zone = self._entity_to_zone(entity_id)
                    return self.volume_up(zone)
                if service == "volume_down":
                    zone = self._entity_to_zone(entity_id)
                    return self.volume_down(zone)

            # Pass-through for anything else
            return self._call_service(domain, service, service_data)

        logger.warning("Unknown audio intent action: %s", action)
        return {"error": f"Unknown action: {action}"}

    def _entity_to_zone(self, entity_id: str) -> str:
        """Reverse lookup: entity ID → zone name (or return entity_id as-is)."""
        reverse = {v: k for k, v in ZONE_ENTITIES.items()}
        return reverse.get(entity_id, entity_id)

    @staticmethod
    def _slugify(text: str) -> str:
        text = (text or "").strip().lower().replace("-", " ")
        return re.sub(r"[^a-z0-9]+", "_", text).strip("_")

    @classmethod
    def _tokenize(cls, *parts: str) -> List[str]:
        tokens = []
        for part in parts:
            slug = cls._slugify(part)
            if slug:
                tokens.extend([t for t in slug.split("_") if t])
        return tokens

    @classmethod
    def _is_group_entity(cls, entity_id: str, friendly_name: str) -> bool:
        tokens = cls._tokenize(entity_id.replace("media_player.", ""), friendly_name)
        token_set = set(tokens)
        if entity_id in ("media_player.whole_house", "media_player.downstairs", "media_player.bedrooms"):
            return True
        if "_group" in entity_id or "group" in token_set:
            return True
        if {"whole", "house"}.issubset(token_set):
            return True
        if "downstairs" in token_set or {"ground", "floor"}.issubset(token_set):
            return True
        if "bedrooms" in token_set or {"bedroom", "group"}.issubset(token_set):
            return True
        if "all" in token_set and ("speaker" in token_set or "speakers" in token_set or "audio" in token_set):
            return True
        return False

    @classmethod
    def _is_echo_entity(cls, entity_id: str, friendly_name: str) -> bool:
        tokens = cls._tokenize(entity_id.replace("media_player.", ""), friendly_name)
        token_set = set(tokens)
        return bool(token_set & {"echo", "alexa"})

    @classmethod
    def _is_wiim_entity(cls, entity_id: str, friendly_name: str) -> bool:
        tokens = cls._tokenize(entity_id.replace("media_player.", ""), friendly_name)
        token_set = set(tokens)
        return "wiim" in token_set

    @classmethod
    def _zone_aliases_for_group(cls, entity_id: str, friendly_name: str) -> List[str]:
        tokens = set(cls._tokenize(entity_id.replace("media_player.", ""), friendly_name))
        aliases = []
        if {"whole", "house"}.issubset(tokens) or "whole_house" in cls._slugify(friendly_name):
            aliases.extend(["whole_house", "whole house", "everywhere", "all"])
        elif "downstairs" in tokens or {"ground", "floor"}.issubset(tokens):
            aliases.append("downstairs")
        elif "bedrooms" in tokens or "bedroom" in tokens:
            aliases.append("bedrooms")
        else:
            slug = cls._slugify(friendly_name) or cls._slugify(entity_id.replace("media_player.", ""))
            if slug:
                aliases.extend([slug, slug.replace("_", " ")])
        return list(dict.fromkeys(aliases))

    @classmethod
    def _zone_aliases_for_echo(cls, entity_id: str, friendly_name: str) -> List[str]:
        raw_entity = entity_id.replace("media_player.", "")
        slug = cls._slugify(raw_entity)
        prefixes = ("echo_dot_", "echo_pop_", "echo_show_", "echo_", "alexa_media_")
        room_slug = ""
        for prefix in prefixes:
            if slug.startswith(prefix):
                room_slug = slug[len(prefix):]
                break
        if not room_slug or room_slug.startswith("media_player"):
            friendly_slug = cls._slugify(friendly_name)
            removable = {"echo", "dot", "pop", "show", "alexa", "media", "player", "speaker", "speakers"}
            room_tokens = [t for t in friendly_slug.split("_") if t and t not in removable]
            room_slug = "_".join(room_tokens)
        aliases = []
        if room_slug:
            aliases.extend([room_slug, room_slug.replace("_", " ")])
        return list(dict.fromkeys([a for a in aliases if a]))

    # -------------------------------------------------------------------
    # Entity auto-discovery
    # -------------------------------------------------------------------
    def sync_entities(self) -> Dict:
        """
        Query HA REST API for all media_player entities, print a summary,
        and return a dict of {friendly_name: entity_id} for discovered devices.

        Use this to replace hardcoded entity IDs in ZONE_ENTITIES after
        Alexa Media Player and WiiM integration have been set up in HA.

        Returns:
            {"entities": [...], "groups": [...], "wiim": [...],
             "echos": [...], "suggested_zones": {...}}
        """
        logger.info("Querying HA for media_player entities...")
        url = f"{self.ha_url}/api/states"
        resp = self._requests.get(url, headers=self.headers, timeout=15)
        resp.raise_for_status()
        all_states = resp.json()

        media_players = [s for s in all_states if s["entity_id"].startswith("media_player.")]

        groups = []
        echos = []
        wiim_devices = []
        others = []

        for state in media_players:
            eid = state["entity_id"]
            attrs = state.get("attributes", {})
            friendly = attrs.get("friendly_name", eid)
            item = {
                "entity_id": eid,
                "friendly_name": friendly,
                "state": state.get("state", "unknown"),
            }
            if self._is_group_entity(eid, friendly):
                groups.append(item)
            elif self._is_echo_entity(eid, friendly):
                echos.append(item)
            elif self._is_wiim_entity(eid, friendly):
                wiim_devices.append(item)
            else:
                others.append(item)

        # Build suggested zone mapping from discovered entities
        suggested_zones = {}
        for w in wiim_devices:
            suggested_zones["wiim"] = w["entity_id"]
            suggested_zones["hi-fi"] = w["entity_id"]
            suggested_zones["hifi"] = w["entity_id"]
        for g in groups:
            for alias in self._zone_aliases_for_group(g["entity_id"], g["friendly_name"]):
                suggested_zones[alias] = g["entity_id"]
        for e in echos:
            for alias in self._zone_aliases_for_echo(e["entity_id"], e["friendly_name"]):
                suggested_zones[alias] = e["entity_id"]

        result = {
            "groups": groups,
            "echos": echos,
            "wiim": wiim_devices,
            "others": others,
            "total": len(media_players),
            "suggested_zones": suggested_zones,
        }

        logger.info("Found %d media_player entities: %d groups, %d Echos, %d WiiM",
                    len(media_players), len(groups), len(echos), len(wiim_devices))
        return result

    def print_entity_discovery(self) -> None:
        """Run sync and print a human-readable report + YAML snippet."""
        result = self.sync_entities()
        print("\n=== Claudette Home — Entity Discovery Report ===")
        print(f"Total media_player entities found: {result['total']}\n")

        def section(title, items):
            if not items:
                print(f"  {title}: none found")
                return
            print(f"  {title}:")
            for item in items:
                print(f"    - {item['entity_id']}  ({item['friendly_name']}) [{item['state']}]")

        section("HA Groups (whole_house, downstairs, etc.)", result["groups"])
        section("WiiM streamers", result["wiim"])
        section("Echo Dots", result["echos"])
        section("Other media players", result["others"])

        zones = result["suggested_zones"]
        if zones:
            print("\n  Suggested ZONE_ENTITIES mapping (paste into code or env):")
            for name in sorted(zones.keys())[:15]:
                print(f"    {name!r}: {zones[name]!r},")
        print()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_controller(stub: bool = False) -> Union[AudioController, AudioControllerStub]:
    """
    Return the appropriate controller.
    stub=True: in-memory fake for tests (no HA required)
    stub=False: real controller (requires HA_URL + HA_TOKEN)
    """
    if stub:
        return AudioControllerStub()
    return AudioController()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Claudette Home — Whole-Home Audio Controller")
    parser.add_argument("--stub", action="store_true", help="Use stub (no HA required)")
    parser.add_argument(
        "--action",
        required=True,
        choices=["announce", "doorbell", "play", "pause", "stop", "volume", "status", "sync"],
        help="Action to perform",
    )
    parser.add_argument("--message", default="", help="TTS message for announce/doorbell")
    parser.add_argument("--zone", default="whole_house", help="Zone name or entity ID")
    parser.add_argument("--level", type=float, help="Volume level (0.0–1.0) for volume action")
    parser.add_argument("--source", help="Playback source (spotify, airplay, etc.) for play action")
    parser.add_argument("--content-id", help="Spotify URI or media URL for play action")
    parser.add_argument("--json-out", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    ctrl = get_controller(stub=args.stub)

    if args.action == "announce":
        if not args.message:
            print("ERROR: --message required for announce", file=sys.stderr)
            sys.exit(1)
        result = ctrl.announce(args.message, zone=args.zone)

    elif args.action == "doorbell":
        msg = args.message or "Someone is at the front door."
        result = ctrl.doorbell_announce(message=msg)

    elif args.action == "play":
        result = ctrl.play(zone=args.zone, source=args.source, content_id=args.content_id)

    elif args.action == "pause":
        result = ctrl.pause(zone=args.zone)

    elif args.action == "stop":
        result = ctrl.stop(zone=args.zone)

    elif args.action == "volume":
        if args.level is None:
            print("ERROR: --level required for volume action", file=sys.stderr)
            sys.exit(1)
        result = ctrl.set_volume(args.zone, args.level)

    elif args.action == "status":
        result = ctrl.status(zone=args.zone)

    elif args.action == "sync":
        if args.stub:
            print("ERROR: --action sync requires a real HA connection (remove --stub)", file=sys.stderr)
            sys.exit(1)
        ctrl.print_entity_discovery()
        result = None

    else:
        print(f"Unknown action: {args.action}", file=sys.stderr)
        sys.exit(1)

    if args.json_out:
        print(json.dumps(result, indent=2))
    elif result:
        print("OK:", json.dumps(result))
    else:
        print("OK")


if __name__ == "__main__":
    main()
