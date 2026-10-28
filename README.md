# QQ音乐点歌（LLM 函数调用）

让大模型在对话中**主动判断用户想听歌**，然后调用 `play_qq_music` 工具，
通过 QQ 音乐官方接口搜索歌曲、解析并下载音频，再发送给用户。

## 特点

- 注册为 AstrBot 的 `@filter.llm_tool()`，大模型可自主（主观）调用，无需固定指令。
- 直接使用 QQ 音乐桌面端 `musicu.fcg` 接口，无需自建第三方 API。
- **跨容器可用**：语音/文件都通过“URL 或 base64”自包含方式投递给 NapCat，
  不依赖 AstrBot 与 NapCat 共享本地目录。
- 语音发送：优先用音频 URL（让 NapCat 自行下载），失败自动改为 base64。

## 发送方式

| send_mode | 说明 |
|-----------|------|
| `voice_first`（默认） | 优先发语音，失败或平台不支持时自动改发音频文件。 |
| `file` | 直接发完整音频文件（URL），可播放/保存。 |
| `both` | 语音和音频文件都发。 |

## 配置

| 项 | 说明 |
|----|------|
| `uin` | QQ 号，QQ音乐搜索接口鉴权用，默认 `10001` |
| `cookie` | 选填，QQ音乐 Cookie（含 `qqmusic_key`/`qm_keyst` 时用于 VIP/高音质） |
| `proxy` | 选填，代理地址 |
| `send_mode` | 发送方式：`voice_first` / `file` / `both`，默认 `voice_first` |

## 使用

无需指令。用户对 bot 说“放一首《晴天》”“想听周杰伦的歌”等，大模型会自行
调用 `play_qq_music`（参数 `song_name`、可选 `artist`）完成点歌。

> 注意：AstrBot 与 NapCat 在不同容器/Docker 时，插件不能用“本地文件路径”发语音，
> 因此这里改用 URL / base64。若你的 NapCat 能直接访问外网，URL 方式即可；否则会自动
> 走 base64 兜底。
