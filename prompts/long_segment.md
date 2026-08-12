You write exactly one light-weight story card in an already planned long video.

Do not write camera directions, timestamped beats, sound design, or an H3 prompt. The selected card will later be opened in the existing single-shot Prompt Studio, which owns all detailed audiovisual writing.

Return one JSON object only:

{
  "segment": {
    "title": "Chinese short card title",
    "boundary_before": "continuous",
    "story_text": "Chinese visible story action and dramatic purpose for this short segment",
    "dialogue": [{"speaker": "name", "language": "Chinese", "text": "exact text"}],
    "opening_state": "precise narrative and visible state at the opening",
    "ending_state": "precise narrative and visible state at the final frame",
    "present_characters": [],
    "covered_outline_chapters": [1],
    "fulfilled_ending_requirements": []
  },
  "warnings": []
}

Requirements:
- Segment 1 uses boundary_before=start. Later segments use continuous unless the story explicitly requires a discontinuous scene/time cut.
- The supplied frames and duration are authoritative. Keep the amount of story realistically performable within this approximately five-second card, but do not write timestamps yet.
- `story_target` is authoritative. Return its exact chapter_numbers and copy its required_ending_conditions byte-for-byte.
- If must_close_story is true, close the whole story in this card without an unresolved next objective.
- Preserve every assigned exact dialogue line byte-for-byte, in its original language, and use it once only. Never translate or rewrite it.
- opening_state must continue from previous_state for a continuous card. ending_state must be concrete enough to describe the next segment's planned Picture 1.
- Prefer one continuous take when this card is later expanded. Do not imply an internal cut merely because several consecutive actions occur.
- Do not output beats, shots, visual, camera, sound, music, or any MiniMax H3 field.
