#!/usr/bin/env bash
# 手工打 APK：aapt2 → javac → d8 → zipalign → apksigner
#
# 为什么不用 Gradle：
#   这个 App 只有一个 WebView Activity，没有第三方库、没有复杂资源。
#   Gradle + AGP 要拉几百 MB 依赖，而这条链路整个构建 3 秒完事，
#   依赖只有 JDK 和 build-tools，在服务器上可复现。
#
# 用法：
#   bash deploy/build_apk.sh                 # 出包到 ~/build/out/Meeting Scribe.apk（版本取自 VERSION）
#   bash deploy/build_apk.sh --version 1.0.1 # 临时出指定版本（覆盖 VERSION，慎用）
set -euo pipefail

APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
ANDROID=$APP_DIR/android
B=$HOME/build
OPT=$B/opt
SDK=$OPT/android-sdk
BT=$SDK/build-tools/34.0.0
PLATFORM=$SDK/platforms/android-34/android.jar
export JAVA_HOME=$OPT/jdk-17.0.20.1+1
PATH=$JAVA_HOME/bin:$PATH
OUT=$B/out
BUILD=$B/build-apk

# 版本号：唯一真源是仓库根的 VERSION 文件（服务端 /api/health 读的也是它）。
# --version 只用于临时出一个不同版本，不要长期靠它——否则真源就形同虚设。
if [ -s "$APP_DIR/VERSION" ]; then
  VERSION=$(tr -d '[:space:]' < "$APP_DIR/VERSION")
else
  echo "❌ 缺 $APP_DIR/VERSION（版本号唯一真源，别手写版本号）"
  exit 1
fi

APP_HOME="${APP_HOME:-}"      # 入口地址；也可用环境变量给

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --home)    APP_HOME="$2"; shift 2 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

# ============================================================================ #
# 入口地址（HOME）：**源码里永远是占位符，打包时才注入。**
#
# 为什么不写在源码里：
#   HOME 是部署事实（这台机器跑在哪个域名），不是代码事实。写进源码会造成两个后果：
#     ① 私有仓和开源仓必须在这一行分叉 —— 导出脚本多一处改写，忘一次就泄露；
#     ② 换域名要改代码再提交，纯配置的事情被做成代码变更。
#   改成构建期注入后，两仓这一行**永远相同**（都是占位符），分叉点彻底消失。
#
# 用法：bash deploy/build_apk.sh --home https://你的域名/
#   或   APP_HOME=https://你的域名/ bash deploy/build_apk.sh
#
# ⚠️ 不给值就**拒绝打包**，不写默认值。默认值会让「忘了给」变成「打出一个指向
#    别人服务器的包」，而那要等装上才被发现。
# ============================================================================ #
MAIN_JAVA="$ANDROID/java/cn/example/meeting/MainActivity.java"
[ -f "$MAIN_JAVA" ] || { echo "❌ 找不到 $MAIN_JAVA（包名目录被改过？）"; exit 1; }

if [ -z "$APP_HOME" ]; then
  echo "❌ 缺入口地址。APK 打开哪个服务由它决定，必须显式给："
  echo "     bash deploy/build_apk.sh --home https://你的域名/"
  echo "     APP_HOME=https://你的域名/ bash deploy/build_apk.sh"
  exit 1
fi
case "$APP_HOME" in
  http://*|https://*) : ;;
  *) echo "❌ --home 必须是完整 URL（http:// 或 https:// 开头），当前：$APP_HOME"; exit 1 ;;
esac

# 源码必须是占位符（否则说明有人把真实域名提交了）
PLACEHOLDER='https://<你的域名>/'
if ! grep -qF "$PLACEHOLDER" "$MAIN_JAVA"; then
  echo "❌ $MAIN_JAVA 里的 HOME 不是占位符。"
  echo "   源码应当写成 $PLACEHOLDER，真实域名由 --home 在打包时注入。"
  echo "   否则私有仓与开源仓会分叉，导出时就可能把真实域名带出去。"
  exit 1
fi

for f in "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner" "$PLATFORM" "$JAVA_HOME/bin/javac"; do
  [ -e "$f" ] || { echo "❌ 缺组件：$f"; exit 1; }
done
[ -f "$ANDROID/AndroidManifest.xml" ] || { echo "❌ 找不到 $ANDROID/AndroidManifest.xml"; exit 1; }

mkdir -p "$OUT" "$BUILD"
rm -rf "$BUILD"/*

echo "=== 1/6 编译资源 ==="
"$BT/aapt2" compile --dir "$ANDROID/res" -o "$BUILD/res.zip"
ls -lh "$BUILD/res.zip" | awk '{print "  res.zip", $5}'

echo "=== 2/6 链接资源（生成 R.java）==="
LINK_ARGS=(
  -o "$BUILD/base.apk"
  -I "$PLATFORM"
  --manifest "$ANDROID/AndroidManifest.xml"
  -R "$BUILD/res.zip"
  --java "$BUILD/gen"
  --min-sdk-version 24
  --target-sdk-version 34
  --auto-add-overlay
)
if [ -n "$VERSION" ]; then
  LINK_ARGS+=(--version-code "$(echo "$VERSION" | tr -d '.')" --version-name "$VERSION")
fi
"$BT/aapt2" link "${LINK_ARGS[@]}"
echo "  base.apk $(du -h "$BUILD/base.apk" | cut -f1)"

echo "=== 3/6 编译 Java ==="
# --release 11：android.jar 里没有 java.* 类，java.* 从 JDK 取（AGP 也是这么做的）。
# 11 而不是 17，是因为 d8 对 11 的 desugar 最成熟，lambda 一定能转成匿名类。
#
# 🔴 编译的是**构建副本**（$BUILD/src），不是 $ANDROID/java 原件。
#    注入 HOME 只改副本，源码里始终是占位符 —— 这样打包不会把工作区弄脏，
#    也不会有人「顺手把真实域名提交上去」。
rm -rf "$BUILD/src"
cp -r "$ANDROID/java" "$BUILD/src"
INJECTED=0
while IFS= read -r -d '' f; do
  if grep -qF "$PLACEHOLDER" "$f"; then
    # 用 | 作分隔符，URL 里的 / 不用转义；
    # 占位符本身也不含 . * [ ] ^ $ 这些 sed 元字符，可直接替换。
    # ⚠️ 若以后改占位符、引入了元字符，这里必须跟着加转义。
    sed -i "s|$PLACEHOLDER|$APP_HOME|g" "$f"
    INJECTED=$((INJECTED + 1))
  fi
done < <(find "$BUILD/src" -name '*.java' -print0)
echo "  注入入口地址：$APP_HOME（改了 $INJECTED 个文件）"
if ! grep -rqF "$APP_HOME" "$BUILD/src"; then
  echo "❌ 注入失败：构建副本里找不到 $APP_HOME"
  exit 1
fi

find "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
find "$BUILD/src" -name '*.java' >> "$BUILD/sources.txt"
echo "  源文件 $(wc -l < "$BUILD/sources.txt") 个"
"$JAVA_HOME/bin/javac" --release 11 -nowarn \
  -classpath "$PLATFORM" -d "$BUILD/classes" "@$BUILD/sources.txt"

echo "=== 4/6 转 dex ==="
find "$BUILD/classes" -name '*.class' > "$BUILD/classes.txt"
echo "  class 文件 $(wc -l < "$BUILD/classes.txt") 个"
mkdir -p "$BUILD/dex"   # d8 的 --output 目录必须已存在，它不会自己建
"$BT/d8" --lib "$PLATFORM" --min-api 24 --output "$BUILD/dex" @"$BUILD/classes.txt"
ls -lh "$BUILD/dex/classes.dex" | awk '{print "  classes.dex", $5}'

echo "=== 5/6 打包 + 对齐 ==="
(cd "$BUILD/dex" && zip -q -u "$BUILD/base.apk" classes.dex)
"$BT/zipalign" -f 4 "$BUILD/base.apk" "$BUILD/aligned.apk"
echo "  aligned.apk $(du -h "$BUILD/aligned.apk" | cut -f1)"

echo "=== 6/6 签名 ==="
KS_DIR=$B/keystore
KS=$KS_DIR/meeting.jks
# 口令不写进源码：**公开仓库里出现的口令等于已泄露**，而且这个口令会随 APK 一起分发出去。
# 从环境变量或 $KS_DIR/.pass 读，缺失就立刻失败——不给默认值，默认值就是一种泄漏。
KS_PASS="${KEYSTORE_PASS:-}"
if [ -z "$KS_PASS" ] && [ -f "$KS_DIR/.pass" ]; then
  KS_PASS=$(tr -d '\r\n' < "$KS_DIR/.pass")
fi
if [ -z "$KS_PASS" ]; then
  echo "❌ 未提供 keystore 口令。" >&2
  echo "   二选一：export KEYSTORE_PASS=...  或  echo '口令' > $KS_DIR/.pass && chmod 600 $KS_DIR/.pass" >&2
  echo "   （.pass 必须在 .gitignore 里，别提交）" >&2
  exit 1
fi
if [ ! -f "$KS" ]; then
  echo "  首次构建，生成签名密钥（**这个文件必须备份，丢了就没法给老用户升级**）"
  mkdir -p "$KS_DIR"
  # DN 里不填真名和住址：证书会被烧进每一个 APK，别人 apksigner verify 就能读出来。
  # 填成项目名即可，DName 对 Android 升级判定毫无影响（认的是「包名 + 密钥对」）。
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -alias meeting \
    -keyalg RSA -keysize 2048 -validity 36500 \
    -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -dname "CN=Meeting Scribe, OU=Personal, O=Personal, L=Unknown, ST=Unknown, C=CN" \
    >/dev/null 2>&1
  chmod 600 "$KS"
fi
APK="$OUT/Meeting Scribe.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:$KS_PASS" \
  --key-pass "pass:$KS_PASS" --v1-signing-enabled true \
  --out "$APK" "$BUILD/aligned.apk"

echo
echo "=== 校验 ==="
"$BT/apksigner" verify --print-certs "$APK" 2>&1 | head -6
"$BT/aapt2" dump badging "$APK" 2>/dev/null | grep -E "^package|application-label|sdkVersion|targetSdkVersion|launchable-activity" | head -6
ls -lh "$APK" | awk '{print "  产物:", $5, $9}'
echo
echo "DONE_APK_OK"
