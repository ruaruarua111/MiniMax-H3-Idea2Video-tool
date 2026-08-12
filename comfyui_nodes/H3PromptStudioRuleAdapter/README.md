# H3 Prompt Studio Rule Adapter

项目自有的六个薄适配节点：读取服务器生成的只读计划、按段路由已保存的首尾帧、将 H3 原生 `17k+5` 帧均匀映射成项目的精确 24fps 帧数，并把完整原生音频时间范围同步映射到精确样本数；另外三个输出节点把图片、视频或上游产物写入项目自己的 `runs`。

项目输出必须通过 `.h3-idea2video-root` 的固定标记校验，目标必须是 `runs` 下的相对路径，节点返回文件大小与 SHA-256 回执。这里不包含模型调用、提示词生成或工作流调度。

上游 `ComfyUI-MiniMaxH3-Contex-Loop` 保持原样；适配节点只复用它的 Loop Start / Current / Segment Save / Loop End / Assemble。
