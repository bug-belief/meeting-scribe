# APK 打包与分发

> 打包在 **<你的主机> 上做**，不占 Windows 的 C 盘。整条链路 3 秒出包。

## 一、为什么不用 Gradle

这个 App 只有一个 `WebView` Activity：没有第三方库、没有复杂资源、没有多模块。
Gradle + AGP 要拉几百 MB 依赖（而且 `services.gradle.org` 在国内实测 5 分钟只下来 4 MB），
而 `aapt2 → javac → d8 → zipalign → apksigner` 这条手工链路：

- 依赖只有 JDK 17 + Android build-tools
- 构建 3 秒
- 每一步都能看懂、能改

40 行的壳用 40 行的构建脚本就够了。

## 二、环境位置

全部在 `~/build/`，**没用 sudo、没碰系统目录**：

| 组件 | 路径 |
|---|---|
| JDK 17 | `~/build/opt/jdk-17.0.20.1+1`（清华 Adoptium 镜像） |
| Android SDK | `~/build/opt/android-sdk`（cmdline-tools 11076708） |
| build-tools | `~/build/opt/android-sdk/build-tools/34.0.0` |
| platform | `~/build/opt/android-sdk/platforms/android-34` |
| **签名密钥** | `~/build/keystore/meeting.jks` |
| 构建产物 | `~/build/out/录音转写.apk` |

装的时候是 `systemd-run --user --unit=install-sdk bash /tmp/install_sdk.sh` 跑的——
**不要用 `nohup`**，SSH 一断进程就没了。

## 三、打包

```bash
cd ~/app
bash deploy/build_apk.sh                    # 出 1.1.0
bash deploy/build_apk.sh --version 1.0.1    # 换版本号
```

产物：`~/build/out/录音转写.apk`（约 29 KB）。

脚本六步，每步都会打印结果，失败会直接退出：

1. `aapt2 compile` 编译 `res/` 资源
2. `aapt2 link` 出 `base.apk` + `R.java`
3. `javac --release 11` 编译 Java（`android.jar` 里没有 `java.*`，从 JDK 取；用 11 是因为 d8 对它的 desugar 最成熟）
4. `d8` 转 `classes.dex`（**输出目录要先建好，d8 不会自己建**）
5. `zipalign -f 4`
6. `apksigner sign`

## 四、签名密钥是命根子

```
~/build/keystore/meeting.jks    别名 meeting
口令放在 ~/build/keystore/.pass（chmod 600，已在 .gitignore 里）
或构建时 export KEYSTORE_PASS=...
```

**口令和证书指纹都不要写进仓库。** 口令写进文档等于已泄露（而且会随 APK 分发给别人）；
指纹虽然不是密钥，但它是「这枚证书的公开身份证」——公开它等于公开「哪些包是我签的」。
需要核对某份包是不是自己签的，本机跑：

```bash
apksigner verify --print-certs 某个.apk
```

**这个文件丢了会怎样**：已经装过的人无法升级——Android 只允许「同签名」覆盖安装。

> ⚠️ **生成时别在 DN 里写真名和住址**（`-dname "CN=你的名字, L=你的城市"`）。
> 证书会烧进每一个 APK，任何人 `apksigner verify --print-certs` 都能读出来。
> 填项目名就够了——Android 判定「能否升级」认的是**包名 + 密钥对**，跟你叫什么都无关。
只能让他先卸载再装新的，而卸载会把口令 cookie 一起清掉，等于每个人都要重新输一次口令。

所以要像 `.env` 一样对待它：

- 和 `.env` 一起列进你自己的「换机必拷」清单
- 至少留一份在本机之外的加密位置

有效期 100 年（`-validity 36500`），不会因为过期失效。

## 五、分发与安装

发 `录音转写-1.1.0.apk` 给家人，对方在手机上：

1. 点开文件 → 系统提示「未知来源应用」，选择「允许来自此来源」
2. 安装完成 → 桌面出现蓝色声波图标
3. 第一次打开是口令输入页 → 输入你给他的口令 → 进去以后不再问

口令存成 cookie（90 天）。到期或清了 App 数据，再输一次即可。

## 六、改域名要重新打包

线上地址在 `android/java/cn/example/meeting/MainActivity.java` 里，但**源码写的是占位符**：

```java
private static final String HOME = "https://<你的域名>/";
```

真实域名**不写进源码**，打包时注入：

```bash
bash deploy/build_apk.sh --home https://你的域名/
# 或
APP_HOME=https://你的域名/ bash deploy/build_apk.sh
```

脚本会把 `android/java` 复制到构建目录，只在**副本**里替换这一行，再编译副本。
源码不动。

**为什么不直接改源码**：这一行是「这台机器跑在哪个域名」，属于部署事实而非代码事实。
写进源码会带来两个麻烦 —— ① 私有仓和开源仓必须在这一行分叉，导出时多一处改写、
忘一次就把域名带出去；② 换域名要改代码再提交。改成注入后，两仓这一行永远相同。

三道护栏，任一不过就拒绝打包：没给 `--home` / 不是完整 URL / 源码里不是占位符。

**功能改动不需要重新打包**——壳加载的是线上网页，你改了服务端，用户打开就是新版。
这是把 App 做成 WebView 壳最大的好处，也是这次选择它的主要原因。

## 七、三个容易踩的点

| 现象 | 原因 | 已在代码里处理 |
|---|---|---|
| 转写正常但**助理不出声** | `setMediaPlaybackRequiresUserGesture` 默认 `true`，脚本触发的播放被当自动播放拦掉 | 设为 `false` |
| 页面一直**连不上麦克风** | WebView 的 `getUserMedia` 要宿主在 `onPermissionRequest` 里明确 grant | 已实现，且先申请系统 `RECORD_AUDIO` |
| 每次打开都要**重输口令** | `CookieManager` 只在内存，进程被杀就没了 | `onPause` 里 `flush()` |

## 八、当前产物

| 项 | 值 |
|---|---|
| 包名 | `cn.example.meeting`（换成你自己的反域名） |
| 版本 | 1.1.0（versionCode 110） |
| minSdk / targetSdk | 24 / 34（Android 7.0 起） |
| 体积 | 29 KB |
| 权限 | INTERNET / RECORD_AUDIO / MODIFY_AUDIO_SETTINGS / ACCESS_NETWORK_STATE / WAKE_LOCK |
| 明文流量 | 禁止（全走 HTTPS，getUserMedia 也要求） |

> **关于包名**
>
> 三处容易踩：
>
> 1. **中间那段不能带连字符** —— Java 包名是标识符，禁连字符，编译直接报错。
> 2. **反域名要写你自己的**，别用文档里的示例。包名一旦发出去就是应用身份，
>    和后装的应用撞名会互相覆盖安装。
> 3. **改包名 = 换应用身份**，Android 用「包名 + 签名」认应用，新包名与旧版**并存不覆盖**。
>    可以借这一点平滑迁移：先装新版验证，确认没问题再卸旧版。
>    代价是新 App 的 WebView cookie 要重新输一次口令（`WEB_TOKEN`）。
>
> 自己打包请换成你的包名（`AndroidManifest.xml` 的 `package`
> 与 `java/` 下的目录结构**要一起改**，漏一个就编译不过）。



