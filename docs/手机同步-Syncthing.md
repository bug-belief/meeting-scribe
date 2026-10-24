# 手机端同步（Syncthing）

> 首次整理：原根目录 `README.md` 第 8 节（2026-10-20 拆分）。原文逐字保留，未改写。

## 8. 手机端同步（Syncthing）

### 8.1 电脑端（Ubuntu）

```bash
sudo apt install -y syncthing
sudo systemctl enable --now syncthing@$USER.service
# 本机浏览器打开 http://127.0.0.1:8384
# 远程访问建议用 SSH 隧道，不要把 8384 暴露公网：
# ssh -L 8384:127.0.0.1:8384 user@server
```

在 Web UI 里：

1. 「添加文件夹」→ 路径填 `<家目录>/data/audios_inbox`，Folder ID 记下来（如 `meeting-audio`）。
2. 文件夹类型选 **Receive Only（仅接收）**。
3. 「操作 → 高级 → 忽略模式」可不填（程序自己会过滤 `.syncthing.*.tmp` 等临时文件）。

### 8.2 Android

1. 安装 **Syncthing-Fork**（F-Droid 或 Play 商店）。
2. 添加文件夹，指向系统录音目录（常见为 `/storage/emulated/0/Recordings`、`MIUI/sound_recorder`、`录音`）。
3. 类型选 **Send Only（仅发送）**，填同一个 Folder ID，与电脑互加设备（扫二维码）。
4. 省电设置里给 Syncthing 开「无限制 / 允许后台」，否则息屏后不传。

### 8.3 iOS

App Store 没有官方 Syncthing，两个可行做法：

- **Möbius Sync**（付费，约 $5）：兼容 Syncthing 协议，在「文件」App 里选中录音文件夹同步到电脑的同一个 Folder ID。
- 不想付费：用「快捷指令」把录音通过 **SSH/scp** 或 **WebDAV**（如 Alist）上传到 `audios_inbox`；或者用 Nextcloud 客户端的「自动上传」再在服务器上软链到 inbox。

### 8.4 一个必踩的坑：处理后文件被移走

`pipeline.py` 成功后会**把音频从 inbox 移到 processed**。如果电脑端是双向同步，这个删除动作会同步回手机，把手机里的原录音删掉。

**正确做法**：手机端 **Send Only** + 电脑端 **Receive Only**。这样电脑端的删除只在本地生效，不会回传；Syncthing 会在电脑端提示「本地更改 / Out of Sync」，**不要点「Revert Local Changes」**，那是让你把文件下回来的。

如果你希望手机里也永久保留原文件，最稳的是在手机上把录音目录再备份一份到云盘（Syncthing 只做「投递」用）。

---
