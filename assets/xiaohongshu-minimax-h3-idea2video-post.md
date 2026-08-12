# 小红书发布素材：MiniMax H3 Idea2Video

## 配图

`xiaohongshu-minimax-h3-idea2video-promo.png`

> 说明：该图片为概念宣传图，不是产品实机演示图；未使用或仿造 MiniMax、ComfyUI、LM Studio 的官方 Logo 与产品界面。

## 标题

AI 视频前后不接？这个本地开源工具，把 MiniMax H3 长视频串起来了！

## 正文

用 AI 做长视频最崩溃的是什么？

角色变脸、场景跳跃，每生成一段都像重新抽卡……

最近挖到一个开源项目——**MiniMax H3 Idea2Video**。

它不是又一个 Prompt 节点，而是一套 Windows 本地工作台，把 Idea、剧本、分镜、H3 提示词、ComfyUI 生产和最终合片串进同一个可恢复项目。

✨ **它怎么改善长视频连续性？**

✅ **真实尾帧接力**

自动使用上一段实际生成的 H3 原生尾帧，作为下一段的 Picture 1。不能保证模型绝对不漂，但比每段从零生成更连续、更可控。

✅ **从 Idea 到成片的一体化流程**

Idea → 故事圣经与长剧本 → 约 5 秒剧情段 → 分镜与 H3 提示词 → 内容确认 → ComfyUI 串行生成 → 精确帧映射 → 1080×1920 合片。

✅ **可视化本地工作台**

自带 Web UI，支持单段精修、参考图分析、剧情卡片编辑和官方 H3 格式编译，不用一直在 ComfyUI 节点之间来回翻找。

🧱 **为什么它更稳？**

**本地 Qwen 编剧**

默认通过 LM Studio 运行 Qwen3.6-27B，配置 131072 上下文。LLM 只负责剧本创作和 H3 文本编译，不负责猜 ComfyUI JSON，也不依赖云端 LLM API。

**Python 规则驱动**

工作流结构、帧数、图片路由、Seed 和提交顺序由本地代码生成并校验，支持精确帧对齐。

**两种生产方式**

支持普通逐段 API 调度，以及 Context Loop 规则工作流；后者保存 latent、音频与元数据 checkpoint，可以从未完成段继续。

**项目级安全存储**

支持独立 venv，图片和最终视频写入项目自己的 `runs` 目录，不把交付文件散落在 ComfyUI 原生输出目录。

🛠 **Windows 本地启动**

双击 `start.bat` 打开工作台；进入创作或调用 AI 时，程序再按需启动 LM Studio 服务并加载项目模型。

`install_comfyui_nodes.bat` 可安装所需自定义节点。安装采用目录联接，不把某台电脑的绝对路径写进工作流；移动项目后重新运行安装脚本即可。

项目提供离线验证与 Mock 测试，自有代码采用 MIT License。

⚠️ **使用前注意**

它不是在线一键出片工具，需要 Windows、本地 LM Studio，以及已经能够运行 MiniMax H3 的 ComfyUI；真实视频生产还需安装 PyAV。

适合 AI 短剧、MV、叙事长视频创作者，以及想基于 H3 二次开发的 ComfyUI 玩家。

项目指路：https://github.com/ruaruarua111/MiniMax-H3-Idea2Video-tool

## 话题

#AIVideo #MiniMaxH3 #ComfyUI #AI短片 #开源工具 #视频生成 #AIGC #LMStudio #Qwen

