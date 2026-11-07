"""3a: read-only scene-variety check on one completed long AUTO project (2026-11-07).

Run inside the web container (same pattern as held_frame_export.py):

    docker cp scene_variety_check.py tubeflowai-test-web-1:/tmp/
    docker exec -e MODE=candidates tubeflowai-test-web-1 python /tmp/scene_variety_check.py
    docker exec -e MODE=export -e PROJECT=<project_id> -e OUT=/tmp/scene_variety \
        tubeflowai-test-web-1 python /tmp/scene_variety_check.py

Every query runs in a READ ONLY transaction; nothing is written to the database, object storage
or any Provider. The analysis part (``analyze``) is pure Python and can be re-run offline on the
exported ``export.json``:

    python -I scene_variety_check.py --offline export.json <out_dir>
"""

from __future__ import annotations

import csv
import json
import os
import pathlib
import statistics
import sys
from collections import Counter

MIN_VOICED_SENTENCES = 150


# --- pure analysis --------------------------------------------------------------------------


def _mmss(ms: int) -> str:
    seconds = ms / 1000
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


def analyze(export: dict) -> dict:  # type: ignore[type-arg]
    """chapters: [{order, sentence_ids, visual_beat, setting}]; groups: [{sequence, start_ms,
    end_ms, video_blocked, sentences: [{sentence_id, duration_ms}], shots: [{scene, ...}]}]."""
    chapters = export["chapters"]
    groups = sorted(export["groups"], key=lambda item: item["sequence"])
    chapter_of = {
        sentence_id: chapter["order"]
        for chapter in chapters
        for sentence_id in chapter["sentence_ids"]
    }
    voiced = {
        sentence["sentence_id"]: sentence["duration_ms"]
        for group in groups
        for sentence in group["sentences"]
    }
    per_chapter = []
    for chapter in chapters:
        ids = [item for item in chapter["sentence_ids"] if item in voiced]
        per_chapter.append(
            {
                "order": chapter["order"],
                "voiced_sentences": len(ids),
                "voiced_seconds": round(sum(voiced[item] for item in ids) / 1000, 1),
            }
        )
    rows = []
    unmapped = 0
    for group in groups:
        orders = [chapter_of.get(item["sentence_id"]) for item in group["sentences"]]
        unmapped += sum(order is None for order in orders)
        counts = Counter(order for order in orders if order is not None)
        primary = min(counts, key=lambda order: (-counts[order], order)) if counts else None
        rows.append(
            {
                "sequence": group["sequence"],
                "start_ms": group["start_ms"],
                "end_ms": group["end_ms"],
                "primary_chapter": primary,
                "chapters": sorted(counts),
                "shots": len(group["shots"]),
                "scenes": [str(shot.get("scene", "")) for shot in group["shots"]],
            }
        )
    runs: list[list[dict]] = []  # type: ignore[type-arg]
    for row in rows:
        if runs and row["primary_chapter"] is not None and (
            runs[-1][-1]["primary_chapter"] == row["primary_chapter"]
            and runs[-1][-1]["sequence"] + 1 == row["sequence"]
        ):
            runs[-1].append(row)
        else:
            runs.append([row])
    by_order = {chapter["order"]: chapter for chapter in chapters}

    def describe(run: list[dict]) -> dict:  # type: ignore[type-arg]
        chapter = by_order.get(run[0]["primary_chapter"], {})
        scenes = [scene for row in run for scene in row["scenes"]]
        return {
            "chapter": run[0]["primary_chapter"],
            "visual_beat": chapter.get("visual_beat"),
            "setting": chapter.get("setting"),
            "groups": [run[0]["sequence"], run[-1]["sequence"]],
            "group_count": len(run),
            "narration_time": [_mmss(run[0]["start_ms"]), _mmss(run[-1]["end_ms"])],
            "seconds": round((run[-1]["end_ms"] - run[0]["start_ms"]) / 1000, 1),
            "shots": len(scenes),
            "distinct_scene_texts": len({_normalized(scene) for scene in scenes}),
            "scenes": scenes,
        }

    longest = sorted(runs, key=lambda run: (-(run[-1]["end_ms"] - run[0]["start_ms"]),
                                             run[0]["sequence"]))
    all_scenes = [scene for row in rows for scene in row["scenes"]]
    return {
        "project_id": export["project_id"],
        "voiced_sentences": len(voiced),
        "groups": len(rows),
        "video_blocked_groups": sum(bool(group.get("video_blocked")) for group in groups),
        "chapters": len(chapters),
        "chapter_limit_reached": len(chapters) >= export.get("chapter_limit", 24),
        "sentences_per_chapter": _stats([item["voiced_sentences"] for item in per_chapter]),
        "seconds_per_chapter": _stats([item["voiced_seconds"] for item in per_chapter]),
        "groups_per_chapter": _stats(
            [float(sum(row["primary_chapter"] == item["order"] for row in rows))
             for item in per_chapter]
        ),
        "sentences_without_chapter": unmapped,
        "total_shots": len(all_scenes),
        "distinct_scene_texts": len({_normalized(scene) for scene in all_scenes}),
        "longest_runs": [describe(run) for run in longest[:3]],
        "per_chapter": per_chapter,
        "per_group": rows,
    }


def write_outputs(result: dict, out: pathlib.Path) -> None:  # type: ignore[type-arg]
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps({k: v for k, v in result.items() if k != "per_group"}, ensure_ascii=False,
                   indent=1)
    )
    with (out / "groups.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sequence", "start", "end", "primary_chapter", "chapters", "shots",
                         "scenes"])
        for row in result["per_group"]:
            writer.writerow([
                row["sequence"], _mmss(row["start_ms"]), _mmss(row["end_ms"]),
                row["primary_chapter"], " ".join(map(str, row["chapters"])), row["shots"],
                " | ".join(row["scenes"]),
            ])


# --- read-only export (web container) -------------------------------------------------------


def _json(value):  # type: ignore[no-untyped-def]
    return json.loads(value) if isinstance(value, str) else value


def _read_only(session):  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    session.execute(text("SET TRANSACTION READ ONLY"))
    return text


def candidates(session) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    text = _read_only(session)
    rows = session.execute(
        text(
            "select p.id, p.title, r.updated_at, "
            "(select count(*) from authoritative_narration_sentences s "
            " where s.project_id = p.id) as voiced, "
            "(select count(*) from authoritative_narration_groups g "
            " where g.project_id = p.id) as groups "
            "from auto_pipeline_runs r join projects p on p.id = r.project_id "
            "where r.state = 'COMPLETED' and p.production_mode = 'AUTO' "
            "order by r.updated_at desc"
        )
    ).mappings().all()
    result = []
    seen = set()
    for row in rows:
        if row["voiced"] < MIN_VOICED_SENTENCES or row["id"] in seen:
            continue
        seen.add(row["id"])
        run = session.execute(
            text(
                "select id from project_director_runs where project_id = :p "
                "and status = 'SUCCEEDED' order by created_at desc limit 1"
            ),
            {"p": row["id"]},
        ).scalar()
        refs = session.execute(
            text(
                "select kind, prompt_ref, count(*) as n from project_director_units "
                "where run_id = :r group by kind, prompt_ref order by kind, prompt_ref"
            ),
            {"r": run},
        ).mappings().all() if run else []
        result.append(
            {
                "project_id": str(row["id"]),
                "title": row["title"],
                "completed_at": row["updated_at"].isoformat(),
                "voiced_sentences": row["voiced"],
                "groups": row["groups"],
                "director_run_id": str(run) if run else None,
                "director_prompt_refs": {
                    f"{item['kind']}:{item['prompt_ref']}": item["n"] for item in refs
                },
            }
        )
        if len(result) == 10:
            break
    return result


def export_project(session, project_id: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    text = _read_only(session)
    run = session.execute(
        text(
            "select id from project_director_runs where project_id = cast(:p as uuid) "
            "and status = 'SUCCEEDED' order by created_at desc limit 1"
        ),
        {"p": project_id},
    ).scalar_one()
    plan_unit = session.execute(
        text(
            "select id, plan_document, reused_from_unit_id, prompt_ref "
            "from project_director_units where run_id = :r and kind = 'GLOBAL_PLAN'"
        ),
        {"r": run},
    ).mappings().one()
    document, source = plan_unit["plan_document"], plan_unit["reused_from_unit_id"]
    while document is None and source is not None:
        row = session.execute(
            text("select plan_document, reused_from_unit_id from project_director_units "
                 "where id = :u"),
            {"u": source},
        ).mappings().one()
        document, source = row["plan_document"], row["reused_from_unit_id"]
    if document is None:
        raise SystemExit("global plan document not found")
    chapters = _json(document)["chapters"]
    groups = {}
    for row in session.execute(
        text(
            "select g.id, g.sequence, g.start_ms, g.end_ms, g.video_blocked, m.ordinal, "
            "s.id as sentence_id, s.measured_duration_ms "
            "from authoritative_narration_groups g "
            "join authoritative_narration_memberships m on m.group_id = g.id "
            "join authoritative_narration_sentences s on s.id = m.sentence_id "
            "where g.project_id = cast(:p as uuid) order by g.sequence, m.ordinal"
        ),
        {"p": project_id},
    ).mappings():
        group = groups.setdefault(
            row["id"],
            {
                "sequence": row["sequence"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "video_blocked": row["video_blocked"],
                "sentences": [],
                "shots": [],
            },
        )
        group["sentences"].append(
            {"sentence_id": str(row["sentence_id"]), "duration_ms": row["measured_duration_ms"]}
        )
    for row in session.execute(
        text(
            "select ptr.group_id, rev.shots_json from storyboard_director_pointers ptr "
            "join storyboard_director_revisions rev on rev.id = ptr.applied_revision_id "
            "where rev.project_id = cast(:p as uuid)"
        ),
        {"p": project_id},
    ).mappings():
        if row["group_id"] in groups:
            groups[row["group_id"]]["shots"] = [
                {key: shot.get(key) for key in ("order", "scene", "shot_size",
                                                 "camera_viewpoint", "semantic_event",
                                                 "subject_focus")}
                for shot in _json(row["shots_json"])
            ]
    title = session.execute(
        text("select title from projects where id = cast(:p as uuid)"), {"p": project_id}
    ).scalar_one()
    unit_refs = session.execute(
        text("select prompt_ref, count(*) from project_director_units where run_id = :r "
             "and kind = 'SHOT_PLAN' group by prompt_ref"),
        {"r": run},
    ).all()
    return {
        "project_id": project_id,
        "title": title,
        "director_run_id": str(run),
        "plan_prompt_ref": plan_unit["prompt_ref"],
        "unit_prompt_refs": {ref: count for ref, count in unit_refs},
        "chapter_limit": 24,
        "chapters": chapters,
        "groups": list(groups.values()),
    }


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--offline":
        export = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
        result = analyze(export)
        write_outputs(result, pathlib.Path(sys.argv[3]))
        print(json.dumps({k: v for k, v in result.items() if k not in ("per_group",
                                                                         "per_chapter")},
                         ensure_ascii=False, indent=1))
        return
    from tubeflow_test.adapters.persistence.database import Database
    from tubeflow_test.config import Settings

    database = Database(Settings().database_url)
    try:
        with database.session() as session:
            if os.environ.get("MODE") == "candidates":
                print(json.dumps(candidates(session), ensure_ascii=False, indent=1))
                return
            export = export_project(session, os.environ["PROJECT"])
        out = pathlib.Path(os.environ.get("OUT", "/tmp/scene_variety"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "export.json").write_text(json.dumps(export, ensure_ascii=False, indent=1))
        result = analyze(export)
        write_outputs(result, out)
        print(json.dumps({k: v for k, v in result.items() if k not in ("per_group",
                                                                         "per_chapter")},
                         ensure_ascii=False, indent=1))
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
