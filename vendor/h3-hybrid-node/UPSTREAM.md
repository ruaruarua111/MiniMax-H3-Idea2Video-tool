# Upstream and implementation note

- Functional reference: <https://github.com/kitsune123150/minimax-h3-hybrid-cond>
- Inspected: 2026-08-11
- Upstream default branch: `main`
- Upstream repository did not expose a license file in its root when inspected.

For that reason this directory does **not** contain a verbatim mirror.  It contains
a small project-local compatibility implementation written against the public
ComfyUI MiniMax H3 node interfaces.  The public project established the useful
conditioning behavior: keep keyframe latents and reference latents together in
the H3 packed payload.  The local implementation narrows the runtime wrapper so
it changes payloads only when both collections are present.

No file below `ComfyUI` is patched or overwritten.  The installer creates only a
directory junction pointing at `minimax_h3_hybrid`.
