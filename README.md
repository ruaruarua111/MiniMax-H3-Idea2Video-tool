# MiniMax H3 Idea2Video

一个 Windows 本地工作台：从一句 Idea 开始，用 LM Studio 中的本地 Qwen 生成、编辑并拆分长剧本，再把已经确认的 MiniMax H3 提示词按规则交给 ComfyUI 串行生产。

项目的重点不是再增加一个单段提示词节点，而是把下面这条链路放在同一份可恢复的项目状态里：

```text
Idea → 长剧本 → 约 5 秒剧情段 → 分镜与完整 H3 提示词
     → 内容确认 → 逐段 H3 → 真实尾帧接力 → 技术校验 → 总片
```

LLM 只参与创作和 H3 文本编译。内容确认后，工作流结构、帧数、图片路由、Seed 与提交顺序都由 Python 规则生成，不让 LLM 猜 ComfyUI JSON。

## 与类似项目的关系

[ComfyUI-MiniMax-H3-Promptor](https://github.com/1038lab/ComfyUI-MiniMax-H3-Promptor) 和 [ComfyUI-MiniMax-Creator](https://github.com/roadmaus/ComfyUI-MiniMax-Creator) 也是优秀的 H3 工具，但定位不同：它们主要工作在 ComfyUI 节点内，本项目是独立开发的本地网页工作台，侧重“一键从 Idea 到成片”和可回溯的多段连续生产。代码、工作流和实现均为本项目独立开发，不是上述项目的分支。

## 环境准备

需要：

- Windows 10/11；
- Python 3.10 或更高版本，推荐 3.11；
- [LM Studio](https://lmstudio.ai/) 及 `lms` CLI；
- 一个已经能够运行 MiniMax H3 的 ComfyUI；
- LM Studio 模型 `Qwen3.6-27B-Uncensored-HauhauCS-Aggressive`；
- ComfyUI 中已有本项目工作流使用的 H3、Turbo LoRA、VAE、文本编码器和 RealESRGAN 模型。

只写剧本和提示词时，Studio 端仅使用 Python 标准库。**真实视频生产必须安装 PyAV**，它用于排队后的媒体检查和最终合片；建议为本项目建立独立环境：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install av
```

`start.bat` 优先使用项目自己的 `.venv`，其次使用系统 `py -3` 或 `python`。它不会使用或上传 ComfyUI 的 Python 环境。

若当前 Studio Python 没有 PyAV，点击视频生产会在任何 ComfyUI 任务提交前明确报错，不会先耗费 GPU 再到验收阶段失败。

### 为什么默认使用 27B

早期实测 `Qwen3.5-9B-Uncensored-HauhauCS-Aggressive` 在长剧本 JSON、精确时间轴、字段类型和修复指令上容易偏离约束，因此默认改为 27B。配置为 131072 上下文、reasoning `on`、流式输出；没有写死 `max_tokens`。

## 一键启动

双击：

```text
start.bat
```

服务默认监听 `http://127.0.0.1:8794`，就绪后自动打开浏览器。启动脚本不会启动、停止或修改 ComfyUI，也不会自动提交 GPU 任务。

LM Studio 默认使用 `127.0.0.1:1234`。进入创作或点击 AI 操作时，Studio 才通过 `lms` 启动服务并加载项目专属模型实例；点击“确认创作完成”后，只卸载本项目拥有的实例。相关启动方式参考 [LM Studio CLI server start 文档](https://lm-studio.cn/docs/cli/serve/server-start)。

## 端口可配置

编辑 `config.json`，或在网页顶部“本地服务设置”中修改：

```json
{
  "provider": "lmstudio",
  "studio_port": 8794,
  "lmstudio_port": 1234,
  "lmstudio_auto_start": true,
  "comfyui_port": 8188,
  "model": "qwen3.6-27b-uncensored-hauhaucs-aggressive",
  "identifier": "h3-script-editor",
  "context_length": 131072,
  "reasoning": "on",
  "stream": true
}
```

三个端口必须不同，并且只连接本机 `127.0.0.1`。保存设置后重启 Studio 生效。

这里没有 `comfyui_root`：本应用不会读取 ComfyUI 安装目录，也不会要求项目必须放在 ComfyUI 旁边。运行时只访问配置的 ComfyUI HTTP API。

## 安装 ComfyUI 节点

首次使用视频生产前，双击：

```text
install_comfyui_nodes.bat
```

安装器只在你选择的 `ComfyUI\custom_nodes` 中创建三个目录联接：

- 固定版本的 Context Loop；
- MiniMax H3 Hybrid 条件节点；
- 本项目的规则、图片路由和安全项目输出节点。

如果项目不在 ComfyUI 附近，安装器会弹出目录选择器；也可以设置环境变量 `COMFYUI_CUSTOM_NODES`，或直接运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\manage_context_loop_node.ps1 -Action Install -CustomNodesRoot "E:\ComfyUI\custom_nodes"
```

安装器不会覆盖同名目录，也不会结束正在运行的 ComfyUI。安装后请在方便时手动重启一次 ComfyUI。

需要移除这三个目录联接时，双击 `uninstall_comfyui_nodes.bat`。卸载器只删除指向本项目对应源码的已验证联接；遇到同名普通目录或其他目标会拒绝删除。

目录联接很重要：工作流中的 `$IDEA2VIDEO_PROJECT_ROOT` 会从联接目标解析回本项目，而不是把某台电脑的绝对路径写进 JSON。若要移动项目目录，请先运行卸载 BAT，移动完成后再重新安装；安装器不会擅自删除指向旧位置或未知位置的同名联接。

## 图片怎样使用

LM Studio 中的 Qwen 只在你点击“分析参考图”时接收图片；H3 文本编译阶段只使用表单文字和 Picture 的视觉描述。

- T2VA：不需要图片；
- I2VA：需要 Picture 1 时，在具体剧情段中选择图片；
- FL2VA：在具体剧情段中选择 Picture 1 和 Picture 2；
- 连续段：不手选 Picture 1，程序自动使用上一段实际交付的原生尾帧。

浏览器选择的 PNG/JPEG/WebP 会先按 SHA-256 保存到：

```text
runs\<project-id>\assets\
```

项目 JSON 只保存资产哈希和相对路径。应用不再浏览或直接读取 `ComfyUI\input`；提交普通逐段工作流时，所需图片才通过 ComfyUI `/upload/image` API 上传。

## 单段工作台

单段页支持 T2VA、I2VA、FL2VA。视觉风格、人物、光线、镜头、声音等均可留空；至少填写创意概述。

推荐顺序：

1. 选择模式和精确时长；
2. 如有参考图，选择图片并点击“分析参考图”，或手工填写 Picture 描述；
3. 生成并编辑分镜；
4. 编译完整 H3 提示词；
5. 查看校验结果，然后复制或下载。

基础模式严格保持官方三字段顺序：

```text
integrated_multimodal_description:
overall_soundscape:
non_diegetic_music:
```

对白和画面文字保持原语言；精确对白必须逐字保留。

## 从 Idea 生成长视频

推荐流程：

1. 在“长视频项目”填写 Idea，目标时长可留空；
2. Qwen 先生成故事圣经和完整剧情，再按约 5 秒的叙事单元拆段；
3. 最后一段可以更短，程序按 24 fps 在所有段之间平衡精确帧数，不要求总时长是 5 秒或 7 秒的倍数；
4. 点击任一剧情卡片，在下方原单段工作台修改 Shot、Picture 和完整 H3 提示词；
5. 修改第 i 段后，本段旧提示词和依赖它的后续段会失效；可以从 i+1 重新生成到结尾，也可以保留明确选择的剧情锚点；
6. 所有段通过校验后，点击“确认创作完成”，释放 LM Studio 显存；
7. 再明确点击视频生产。此前不会向 ComfyUI 排队。

GPU 阶段不会再调用 Qwen。普通调度器每次只提交一段，等待这一段完成、落盘和校验后，才提交下一段；不会清空或打断已有 ComfyUI 队列。

### 两种执行方式

“普通逐段生产”每段构造一份 API JSON。第一段按 T2VA/I2VA/FL2VA 路由，后续连续段通过 API 上传上一段的真实原生尾帧，并作为下一段 Picture 1。

“规则循环工作流”适合已经完成全部剧本和 H3 提示词的项目。Python 直接从保存内容编译一个递归工作流，不再调用 LLM；Context Loop 保存每段 latent/音频 checkpoint，支持从首个未完成段继续。最终交付仍使用精确帧映射和真实端点。

## 输出位置

工作流不再先写 ComfyUI `output` 再让 Studio 复制。项目输出节点直接把交付文件写到本仓库忽略的 `runs`：

```text
runs\<project-id>\segments\0001\attempt_1\
  tail_native.png
  qc_sample_0001.png
  native_768x1344.mp4
  final_1080x1920.mp4

runs\<project-id>\master\
  <project-id>_master.mp4

runs\<project-id>\context_loop\output\
  native_r000001.mp4
  upscale_segments\clip_0001.mp4
  master_1080x1920.mp4
```

每个项目输出节点返回包含相对路径、字节数和 SHA-256 的回执；Studio 只接受回执中位于本项目 `runs` 下且哈希一致的文件。

Context Loop 为断点恢复保存的 latent、WAV 和元数据 checkpoint 仍由上游节点放在 ComfyUI 内部输出区；这些是内部缓存，不是最终交付。Studio 通过 `/view` 和插件 checkpoint API 读取它们，不直接读取 ComfyUI 目录。原生总片和 1080p 交付写入本项目。

三份手工导入工作流位于 `comfyui_workflows`，也使用项目输出节点，默认写入：

```text
runs\manual_exports\
```

## H3 规格与连续性

默认生产规格：

- `minimax_h3_fl2va_pruned_int8_convrot.safetensors`；
- `minimax_h3_turbo_v4_step600_ema.safetensors`；
- Turbo v4，8 steps；
- 原生 768×1344，最终 1080×1920；
- 24 fps，H.264，CRF 18；
- 保留 H3 原生音频；
- DynamicVRAM 由 ComfyUI 启动参数控制。

H3 常见原生长度满足 `17k+5`。程序将原生序列均匀映射到项目分配的精确帧数：连续段跳过重复的首个条件帧，但始终保留生成序列末端；FL2VA 同时保留 Picture 1 和 Picture 2，因此不会出现“接了尾帧却在裁剪时删掉”的问题。

## 官方规范与 prompts 来源

提示词规范来自 [MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3) 的 [h3-prompt-writing skill](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)。

项目原样保存：

```text
vendor\h3-prompt-writing\SKILL.md
vendor\h3-prompt-writing\references\base-en.txt
vendor\h3-prompt-writing\references\ref-en.txt
```

来源和同步信息见 `vendor\h3-prompt-writing\UPSTREAM.md`。`prompts` 目录中的编译提示参考了这套官方规范，但属于本项目自己的任务提示；vendor 内的官方原文没有改写。

## 离线验证

以下命令不会调用 LM Studio、不会访问真实 ComfyUI、不会排队 GPU：

```bat
py -3 -m py_compile app.py longform.py longform_runtime.py context_loop.py context_runtime.py project_assets.py lmstudio_runtime.py tools\build_workflows.py tools\build_long_workflow.py tools\build_context_workflow.py tests\test_offline.py
```

纯系统 Python 也可以运行离线测试；缺少 Torch/PyAV 时，相应集成项会明确标记为 skipped：

```bat
py -3 tests\test_offline.py
```

若要执行包括自定义节点与媒体合片在内的全部项目，可改用任意已安装 Torch 和 PyAV 的测试环境。该环境只用于测试，不是 Studio 的启动依赖。

测试覆盖端口、LM Studio mock、三种 H3 格式、对白保留、长剧本依赖失效、项目图片、工作流节点/link、项目输出回执、精确帧映射、串行调度、断点恢复和健康检查。

## 安全说明

- 只监听 loopback，不应暴露到公网；
- `--host` 也只接受 `127.0.0.1`、`localhost` 或 `::1`；写接口拒绝跨来源请求和非 JSON 请求；
- 没有 API Key，也不会读取 `.secrets`；
- `runs`、日志、临时文件和虚拟环境不提交；
- 项目输出节点必须看到根目录标记 `.h3-idea2video-root`，且只能写入其下的 `runs`；
- 安装器拒绝覆盖未知 `custom_nodes` 目录；
- 不要同时运行多个重型视频任务，先确认显存和 ComfyUI 队列状态。

## 许可证

本项目自有代码采用 [MIT License](LICENSE)。`vendor` 中保存的上游原文与第三方代码继续适用各自的版权和许可条款，来源与固定版本见相应 `UPSTREAM.md`、`LICENSE`，不由本项目重新授权。
