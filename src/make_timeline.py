#!/usr/bin/env python3
"""
Convert a cutlist into an OpenTimelineIO timeline for DaVinci Resolve.

Resolve imports AAF/EDL/XML/FCPXML/DRT/ADL/OTIO via File > Import > Timeline
(same importer that backs MediaPool.ImportTimelineFromFile). This sidesteps the
scripting API entirely, which is Studio-gated -- see
docs/design-notes.md#interchange-files-not-scripting.

Per the rough-cut design: V1 holds highlights, V2 holds everything else, and V2
clips stay at their ORIGINAL timeline position -- time-aligned with V1, not packed
back-to-back -- so both tracks reference the same absolute timeline. Nothing is
deleted.

With --graphics, the overlay manifest from make_graphics.py goes on a third video
track (G1, which Resolve shows as V3) above both. Each item is a full-frame
transparent PNG with its position baked in, so it lands at 100% with no transform.
Images are referenced by bare absolute path: that form is confirmed to import
(projects/_png_probe/); file:// is untested for stills.

Operating rule 2 applies: refuses to overwrite an existing output without --force.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

import opentimelineio as otio

RT = otio.opentime.RationalTime
TimeRange = otio.opentime.TimeRange

VIDEO_TRACKS = ("V1", "V2")


def frames(seconds, fps):
    """Seconds -> whole frames at fps. Rounded, so segment edges stay on frame
    boundaries rather than inheriting float drift from the signal analysis."""
    return round(float(seconds) * fps)


def load_cutlist(path):
    with open(path) as f:
        cut = json.load(f)

    for key in ("media", "fps", "segments"):
        if key not in cut:
            sys.exit("cutlist is missing required key: %r" % key)

    fps = float(cut["fps"])
    if fps <= 0:
        sys.exit("cutlist fps must be positive, got %r" % cut["fps"])

    segs = []
    for i, s in enumerate(cut["segments"]):
        track = s.get("track", "V1")
        if track not in VIDEO_TRACKS:
            sys.exit("segment %d: track must be one of %s, got %r"
                     % (i, " / ".join(VIDEO_TRACKS), track))
        start_f = frames(s["start"], fps)
        end_f = frames(s["end"], fps)
        if end_f <= start_f:
            sys.exit("segment %d: end (%s) must be after start (%s)"
                     % (i, s["end"], s["start"]))
        segs.append({
            "start_f": start_f,
            "end_f": end_f,
            "track": track,
            "reason": s.get("reason", ""),
        })

    # Overlaps within one track would make the gap arithmetic below wrong, and
    # Resolve would receive clips in an order that doesn't match the cutlist.
    for track in VIDEO_TRACKS:
        on_track = sorted((s for s in segs if s["track"] == track),
                          key=lambda s: s["start_f"])
        for a, b in zip(on_track, on_track[1:]):
            if b["start_f"] < a["end_f"]:
                sys.exit("overlapping segments on %s: %.3fs-%.3fs and %.3fs-%.3fs"
                         % (track, a["start_f"] / fps, a["end_f"] / fps,
                            b["start_f"] / fps, b["end_f"] / fps))

    return cut, fps, segs


def media_target(media_path, url_style):
    """Resolve's preference for target_url is unverified -- 'file' is the OTIO
    spec-correct form, 'path' is the first thing to try if relinking fails."""
    abspath = os.path.abspath(media_path)
    if url_style == "path":
        return abspath
    return urllib.parse.urljoin("file:", urllib.request.pathname2url(abspath))


def load_graphics(path, fps):
    """graphics.json -> overlay items in frames, image paths made absolute."""
    with open(path) as f:
        g = json.load(f)
    base = os.path.dirname(os.path.abspath(path))

    items = []
    for i, it in enumerate(g.get("items", [])):
        image = it["image"] if os.path.isabs(it["image"]) else os.path.join(base, it["image"])
        if not os.path.exists(image):
            sys.exit("graphics item %d: image not found: %s" % (i, image))
        if any(ch.isdigit() for ch in os.path.basename(image)):
            # Resolve reads a digit run in a still's filename as an image-sequence
            # frame number, then shows the clip as media offline.
            sys.exit("graphics item %d: image filename contains a digit, which Resolve "
                     "treats as an image sequence (media offline): %s" % (i, image))
        start_f, end_f = frames(it["start"], fps), frames(it["end"], fps)
        if end_f <= start_f:
            sys.exit("graphics item %d: end (%s) must be after start (%s)"
                     % (i, it["end"], it["start"]))
        items.append({"start_f": start_f, "end_f": end_f, "image": image,
                      "icon": it.get("icon", ""), "said": it.get("said", "")})

    items.sort(key=lambda it: it["start_f"])
    for a, b in zip(items, items[1:]):
        if b["start_f"] < a["end_f"]:
            sys.exit("overlapping graphics: %s at %.3fs and %s at %.3fs -- make_graphics.py "
                     "should never emit this" % (a["icon"], a["start_f"] / fps,
                                                 b["icon"], b["start_f"] / fps))
    return items


def build_graphics_track(items, fps, total_f):
    """Stills on their own video track, Gap/Clip alternating like build_track."""
    track = otio.schema.Track(name="G1", kind=otio.schema.TrackKind.Video)
    playhead_f = 0
    for it in items:
        if it["start_f"] > playhead_f:
            track.append(otio.schema.Gap(source_range=TimeRange(
                start_time=RT(0, fps), duration=RT(it["start_f"] - playhead_f, fps))))
        length = it["end_f"] - it["start_f"]
        # A still has no inherent duration: declare one exactly as long as the use.
        clip = otio.schema.Clip(
            name=os.path.basename(it["image"]),
            media_reference=otio.schema.ExternalReference(
                target_url=it["image"],
                available_range=TimeRange(start_time=RT(0, fps), duration=RT(length, fps))),
            source_range=TimeRange(start_time=RT(0, fps), duration=RT(length, fps)),
        )
        if it["said"]:
            clip.metadata["graphics_trigger"] = it["said"]
        track.append(clip)
        playhead_f = it["end_f"]
    if total_f > playhead_f:
        track.append(otio.schema.Gap(source_range=TimeRange(
            start_time=RT(0, fps), duration=RT(total_f - playhead_f, fps))))
    return track


def build_track(name, segments, fps, total_f, media_url, media_name, clip_duration_f):
    """One video track: Gap/Clip alternating so each clip lands on its absolute
    timeline position."""
    track = otio.schema.Track(name=name, kind=otio.schema.TrackKind.Video)
    playhead_f = 0

    for seg in sorted(segments, key=lambda s: s["start_f"]):
        if seg["start_f"] > playhead_f:
            track.append(otio.schema.Gap(source_range=TimeRange(
                start_time=RT(0, fps),
                duration=RT(seg["start_f"] - playhead_f, fps),
            )))

        # Straight cut from a single source, so source timecode == timeline
        # position. A fresh ExternalReference per clip keeps each clip
        # independently relinkable in Resolve.
        clip = otio.schema.Clip(
            name="%s %.2f-%.2f" % (media_name, seg["start_f"] / fps, seg["end_f"] / fps),
            media_reference=otio.schema.ExternalReference(
                target_url=media_url,
                available_range=TimeRange(start_time=RT(0, fps),
                                          duration=RT(clip_duration_f, fps)),
            ),
            source_range=TimeRange(
                start_time=RT(seg["start_f"], fps),
                duration=RT(seg["end_f"] - seg["start_f"], fps),
            ),
        )
        if seg["reason"]:
            clip.metadata["cutlist_reason"] = seg["reason"]
        track.append(clip)
        playhead_f = seg["end_f"]

    # Trailing gap keeps both tracks the same length, so V1 and V2 stay aligned
    # on the same absolute timeline even when the last highlight ends early.
    if total_f > playhead_f:
        track.append(otio.schema.Gap(source_range=TimeRange(
            start_time=RT(0, fps),
            duration=RT(total_f - playhead_f, fps),
        )))

    return track


def main():
    ap = argparse.ArgumentParser(
        description="Build an OTIO timeline from a cutlist for Resolve import.")
    ap.add_argument("--cutlist", required=True, help="cutlist JSON")
    ap.add_argument("--out", required=True, help="output .otio (use a NEW versioned filename)")
    ap.add_argument("--media", help="override the media path from the cutlist")
    ap.add_argument("--name", help="timeline name (default: output file stem)")
    ap.add_argument("--url-style", choices=["file", "path"], default="file",
                    help="target_url form: file:// URL (default) or bare absolute path")
    ap.add_argument("--no-audio", action="store_true",
                    help="video tracks only; by default A1/A2 mirror V1/V2")
    ap.add_argument("--graphics", help="graphics JSON from make_graphics.py -> overlay track G1")
    ap.add_argument("--force", action="store_true", help="allow overwriting --out")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        sys.exit("refusing to overwrite %s (pass --force, or use a new versioned "
                 "filename -- see operating rule 2)" % args.out)

    cut, fps, segs = load_cutlist(args.cutlist)

    # A path inside the cutlist is relative to the cutlist (so a project folder
    # stays portable); an explicit --media override is relative to the CWD, like
    # any other CLI path argument.
    if args.media:
        media_path = os.path.abspath(args.media)
    else:
        media_path = cut["media"]
        if not os.path.isabs(media_path):
            media_path = os.path.join(os.path.dirname(os.path.abspath(args.cutlist)),
                                      media_path)
    if not os.path.exists(media_path):
        sys.exit("media not found: %s" % media_path)

    media_url = media_target(media_path, args.url_style)
    media_name = os.path.basename(media_path)

    clip_duration_f = (frames(cut["duration"], fps) if "duration" in cut
                       else max(s["end_f"] for s in segs))
    total_f = max(s["end_f"] for s in segs)

    graphics = load_graphics(args.graphics, fps) if args.graphics else []
    if graphics and graphics[-1]["end_f"] > total_f:
        # The hold can carry an icon past the last segment; extend rather than
        # silently trimming it, and say so.
        print("note: graphics run to %.3fs, past the cutlist's %.3fs -- timeline extended"
              % (graphics[-1]["end_f"] / fps, total_f / fps))
        total_f = graphics[-1]["end_f"]

    timeline = otio.schema.Timeline(
        name=args.name or os.path.splitext(os.path.basename(args.out))[0])
    timeline.global_start_time = RT(0, fps)

    for track_name in VIDEO_TRACKS:
        on_track = [s for s in segs if s["track"] == track_name]
        if not on_track:
            continue
        timeline.tracks.append(build_track(track_name, on_track, fps, total_f,
                                           media_url, media_name, clip_duration_f))
        if not args.no_audio:
            audio = build_track("A" + track_name[1:], on_track, fps, total_f,
                                media_url, media_name, clip_duration_f)
            audio.kind = otio.schema.TrackKind.Audio
            timeline.tracks.append(audio)

    if graphics:
        timeline.tracks.append(build_graphics_track(graphics, fps, total_f))

    otio.adapters.write_to_file(timeline, args.out, adapter_name="otio_json")

    print("timeline: %s" % timeline.name)
    print("rate:     %g fps" % fps)
    print("duration: %d frames (%.3fs)" % (total_f, total_f / fps))
    print("media:    %s" % media_url)
    for track in timeline.tracks:
        clips = [(i, c) for i, c in enumerate(track) if isinstance(c, otio.schema.Clip)]
        print("  %-3s %-5s %2d clips" % (track.name, str(track.kind), len(clips)))
        for i, c in clips:
            # Timeline position, not source range: for a still the source starts at 0.
            r = track.range_of_child_at_index(i)
            print("        %8.3fs -> %8.3fs  (%s)"
                  % (r.start_time.to_seconds(),
                     r.end_time_exclusive().to_seconds(),
                     c.metadata.get("cutlist_reason") or c.metadata.get("graphics_trigger", "")))
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
