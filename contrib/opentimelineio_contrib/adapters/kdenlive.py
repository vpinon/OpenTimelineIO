#
# Copyright (C) 2019 Vincent Pinon <vpinon@kde.org>
#
# Licensed under the Apache License, Version 2.0 (the "Apache License")
# with the following modification; you may not use this file except in
# compliance with the Apache License and the following modification to it:
# Section 6. Trademarks. is deleted and replaced with:
#
# 6. Trademarks. This License does not grant permission to use the trade
#    names, trademarks, service marks, or product names of the Licensor
#    and its affiliates, except as required to comply with Section 4(c) of
#    the License and to reproduce the content of the NOTICE file.
#
# You may obtain a copy of the Apache License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the Apache License with the above modification is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the Apache License for the specific
# language governing permissions and limitations under the Apache License.
#

"""OpenTimelineIO Kdenlive (MLT) XML Adapter. """
import re
import os
from xml.etree import ElementTree as ET
import opentimelineio as otio


def mlt_property(element, name):
    return element.findtext(f"property[@name='{name}']")


def time(clock, fps):
    hms = [float(x) for x in clock.replace(",", ".").split(":")]
    f = 0
    m = fps if len(hms) > 1 else 1  # no delimiter, it is a frame number
    for x in reversed(hms):
        f = f + x * m
        m = m * 60
    return otio.opentime.RationalTime(round(f, 3), fps)


def keyframes(kfstring, rate):
    # kflist: semicolon (;) separated list of time/value pair
    # separated by = (linear interp) or ~= (spline) or |= (step)
    # becomes a dict with RationalTime keys
    return dict((time(t, rate), v)
                for (t, v) in re.findall("([^|~=;]*)[|~]?=([^;]*)", kfstring))


def read_from_string(xml):
    """
    Import Kdenlive XML project into OTIO timeline
    """
    mlt, byid = ET.XMLID(xml)
    profile = mlt.find("profile")
    rate = float(profile.get("frame_rate_num")) \
        / float(profile.get("frame_rate_den", 1))
    timeline = otio.schema.Timeline(
            name=mlt.get("name", "Kdenlive imported timeline"))

    maintractor = mlt.find("tractor[@global_feed='1']")
    for maintrack in maintractor.findall("track"):
        if maintrack.get("producer") == 'black_track':
            continue
        subtractor = byid[maintrack.get("producer")]
        track = otio.schema.Track(
                name=mlt_property(subtractor, "kdenlive:track_name"))
        if bool(mlt_property(subtractor, "kdenlive:audio_track")):
            track.kind = otio.schema.TrackKind.Audio
        else:
            track.kind = otio.schema.TrackKind.Video
        for subtrack in subtractor.findall("track"):
            playlist = byid[subtrack.get("producer")]
            for item in playlist.iter():
                if item.tag == 'blank':
                    gap = otio.schema.Gap(
                            duration=time(item.get("length"), rate))
                    track.append(gap)
                elif item.tag == 'entry':
                    producer = byid[item.get("producer")]
                    service = mlt_property(producer, "mlt_service")
                    available_range = otio.opentime.TimeRange(
                        start_time=time(producer.get("in"), rate),
                        duration=time(producer.get("out"), rate)
                        - time(producer.get("in"), rate))
                    source_range = otio.opentime.TimeRange(
                        start_time=time(item.get("in"), rate),
                        duration=time(item.get("out"), rate)
                        - time(item.get("in"), rate))
                    # media reference clip
                    reference = None
                    if service in ["avformat", "avformat-novalidate", "qimage"]:
                        reference = otio.schema.ExternalReference(
                                target_url=mlt_property(producer, 'kdenlive:originalurl')
                                or mlt_property(producer, 'resource'),
                                available_range=available_range)
                    elif service == "color":
                        reference = otio.schema.GeneratorReference(
                                generator_kind="SolidColor",
                                parameters={"color": mlt_property(producer, "resource")},
                                available_range=available_range)
                    clip = otio.schema.Clip(
                        name=mlt_property(producer, 'kdenlive:clipname'),
                        source_range=source_range,
                        media_reference=reference or otio.schema.MissingReference())
                    # effect # TODO: waiting for OTIO Effects definition
                    for effect in item.findall("filter"):
                        kdenlive_id = mlt_property(effect, "kdenlive_id")
                        if kdenlive_id == "fadein":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="audio_fade_in",
                                metadata={'duration': time(effect.get("out"), rate) - time(effect.get("in"), rate)}))
                        elif kdenlive_id == "fadeout":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="audio_fade_out",
                                metadata={'duration': time(effect.get("out"), rate) - time(effect.get("in"), rate)}))
                        elif kdenlive_id == "fade_from_black":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="video_fade_in",
                                metadata={'duration': time(effect.get("out"), rate)}))
                        elif kdenlive_id == "fade_to_black":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="video_fade_out",
                                metadata={'duration': time(effect.get("out"), rate) - time(effect.get("in"), rate)}))
                        elif kdenlive_id == "volume":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="volume",
                                metadata={'keyframes': keyframes(mlt_property(effect, "level"), rate)}))
                        elif kdenlive_id == "brightness":
                            clip.effects.append(otio.schema.Effect(
                                effect_name="brightness",
                                metadata={'keyframes': keyframes(mlt_property(effect, "level"), rate)}))
                    track.append(clip)
        timeline.tracks.append(track)

    for transition in maintractor.findall("transition"):
        kdenlive_id = mlt_property(transition, "kdenlive_id")
        if kdenlive_id == "wipe":
            timeline.tracks[int(mlt_property(transition, "b_track"))-1].append(
                    otio.schema.Transition(
                            transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                            in_offset=time(transition.get("in"), rate),
                            out_offset=time(transition.get("out"), rate)))

    return timeline


def add_property(element, name, value):
    property = ET.SubElement(element, "property", {"name": name})
    property.text = value


def write_to_string(timeline):
    if not isinstance(timeline, otio.schema.Timeline) and len(timeline) > 1:
        print("WARNING: Only one timeline supported, using the first one.")
        timeline = timeline[0]
    mlt = ET.Element("mlt", {
        "version": "6.16.0",
        "title": timeline.name,
        "LC_NUMERIC": "en_US.UTF-8",
        "producer": "main_bin"})
    rate = timeline.duration().rate
    (rate_num, rate_den) = {
            23.98: (24000, 1001),
            29.97: (30000, 1001),
            59.94: (60000, 1001)
            }.get(round(float(rate), 2), (int(rate), 1))
    ET.SubElement(mlt, "profile", {
            "description": f"HD 1080p {rate} fps",
            "frame_rate_num": str(rate_num),
            "frame_rate_den": str(rate_den),
            "width": "1920",
            "height": "1080",
            "display_aspect_num": "16",
            "display_aspect_den": "9",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "colorspace": "709",
            "progressive": "1"})

    # build media library, indexed by url
    main_bin = ET.Element("playlist", {"id": "main_bin"})
    add_property(main_bin, "kdenlive:docproperties.decimalPoint", ".")
    add_property(main_bin, "kdenlive:docproperties.version", "0.98")
    add_property(main_bin, "xml_retain", "1")
    media_prod = {}
    for clip in timeline.each_clip():
        service = None
        resource = None
        if isinstance(clip.media_reference, otio.schema.ExternalReference):
            resource = clip.media_reference.target_url
            service = "qimage" if os.path.splitext(resource)[1].lower() \
                in [".png", ".jpg", ".jpeg"] else "avformat"
        elif isinstance(clip.media_reference, otio.schema.GeneratorReference) \
                and clip.media_reference.generator_kind == "SolidColor":
            service = "color"
            resource = clip.media_reference.parameters["color"]
        if not (service and resource) or (resource in media_prod.keys()):
            continue
        producer = ET.SubElement(mlt, "producer", {
            "id": f"producer{len(media_prod)}",
            "in":  str(int(clip.media_reference.available_range.start_time.value)),
            "out": str(int((clip.media_reference.available_range.start_time
                           + clip.media_reference.available_range.duration).value))})
        ET.SubElement(main_bin, "entry", {"producer": f"producer{len(media_prod)}"})
        add_property(producer, "mlt_service", service)
        add_property(producer, "resource", resource)
        if clip.name:
            add_property(producer, "kdenlive:clipname", clip.name)
        media_prod[resource] = producer

    unsupported = ET.SubElement(mlt, "producer", {"id": "unsupported"})
    add_property(unsupported, "mlt_service", "qtext")
    add_property(unsupported, "family", "Courier")
    add_property(unsupported, "fgcolour", "#ff808080")
    add_property(unsupported, "bgcolour", "#00000000")
    add_property(unsupported, "text", "Unsupported clip type")
    add_property(unsupported, "kdenlive:clipname", "Unsupported clip type")
    ET.SubElement(main_bin, "entry", {"producer": "unsupported"})
    mlt.append(main_bin)

    black = ET.SubElement(mlt, "producer", {"id": "black_track"})
    add_property(black, "resource", "black")
    add_property(black, "mlt_service", "color")

    maintractor = ET.Element("tractor", {"global_feed": "1"})
    ET.SubElement(maintractor, "track", {"producer": "black_track"})
    track_count = 0
    for track in timeline.tracks:
        track_count = track_count + 1

        ET.SubElement(maintractor, "track",
                      {"producer": f"tractor{track_count}"})
        subtractor = ET.Element("tractor", {"id": f"tractor{track_count}"})
        add_property(subtractor, "kdenlive:track_name", track.name)

        ET.SubElement(subtractor, "track", {
                "producer": f"playlist{track_count}_1",
                "hide": "audio" if track.kind == otio.schema.TrackKind.Video
                else "video"})
        ET.SubElement(subtractor, "track", {
                "producer": f"playlist{track_count}_2",
                "hide": "audio" if track.kind == otio.schema.TrackKind.Video
                else "video"})
        playlist = ET.SubElement(mlt, "playlist",
                                 {"id": f"playlist{track_count}_1"})
        playlist_ = ET.SubElement(mlt, "playlist",
                                  {"id": f"playlist{track_count}_2"})
        if track.kind == otio.schema.TrackKind.Audio:
            add_property(subtractor, "kdenlive:audio_track", "1")
            add_property(playlist,   "kdenlive:audio_track", "1")
            add_property(playlist_,  "kdenlive:audio_track", "1")

        for item in track:
            if isinstance(item, otio.schema.Gap):
                ET.SubElement(playlist, "blank",
                              {"length": str(int(item.duration().value))})
            elif isinstance(item, otio.schema.Clip):
                if isinstance(item.media_reference,
                              otio.schema.MissingReference):
                    resource = "unhandled_type"
                if isinstance(item.media_reference,
                              otio.schema.ExternalReference):
                    resource = item.media_reference.target_url
                elif isinstance(item.media_reference,
                                otio.schema.GeneratorReference) \
                        and item.media_reference.generator_kind == "SolidColor":
                    resource = item.media_reference.parameters["color"]
                ET.SubElement(playlist, "entry", {
                        "producer": media_prod[resource].attrib["id"]
                        if item.media_reference
                        and not item.media_reference.is_missing_reference
                        else "unsupported",
                        "in": str(int(item.source_range.start_time.value)),
                        "out": str(int((item.source_range.duration
                                        + item.source_range.start_time).value))})
                for effect in item.effects:
                    print("Effects handling to be added")
            elif isinstance(item, otio.schema.Transition):
                print("Transitions handling to be added")
        mlt.append(subtractor)
    mlt.append(maintractor)

    return "<?xml version='1.0' encoding='utf-8'?>\n" \
        + ET.tostring(mlt, encoding="unicode")


if __name__ == "__main__":
    # timeline = otio.adapters.read_from_file("test.kdenlive")
    timeline = read_from_string(open("test.kdenlive", "r").read())
    # print(otio.adapters.write_to_string(timeline, "otio_json"))
    print(str(timeline).replace("otio.schema", "\notio.schema"))
    xml = write_to_string(timeline)
    xml = xml.replace("><", ">\n<")
    # print(xml)
    open('conv.kdenlive', 'w').write(xml)
