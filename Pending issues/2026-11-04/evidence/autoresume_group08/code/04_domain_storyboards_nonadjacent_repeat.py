# source: .../domain/storyboards.py L336-360（非相邻重复校验，异常文案与日志逐字一致）
        if linked_sentences != set(self.group_sentence_ids):
            raise ValueError("every sentence in the group must be covered")
        event_keys = [shot.event_repeat_key() for shot in self.shots]
        composition_keys = [
            (
                _normalized(shot.scene, "scene"),
                _normalized(shot.shot_size, "size"),
                _normalized(shot.camera_viewpoint, "viewpoint"),
            )
            for shot in self.shots
        ]

        def repeats_non_adjacent(values: Sequence[tuple[str, ...]]) -> bool:
            positions: dict[tuple[str, ...], int] = {}
            for index, value in enumerate(values):
                previous = positions.get(value)
                if previous is not None and index - previous > 1:
                    return True
                positions[value] = index
            return False

        if repeats_non_adjacent(event_keys) or repeats_non_adjacent(composition_keys):
            raise ValueError("director plan repeats an event or composition across the group")
        approved_pairs = {
            (item.previous_shot_id, item.next_shot_id)
