#!/bin/bash
# setup_proxy.sh - 多节点轮询解析与 sing-box 启动
# 修复记录 (2026-10-04):
#  [致命] tr '-_' '+/' -> tr -- '-_' '+/' (原写法 tr 直接报错返回空, 导致 vmess 节点全部跳过、Base64 订阅永远解码失败)
#  [致命] 连通性测试改用 socks5h:// (DNS 走代理, 避免本地 DNS 污染误判节点失效)
#  [高]   hysteria2: obfs=类型 / obfs-password=密码 分开解析 (原来把类型当密码)
#  [高]   tuic/socks 的 user:pass 按第一个冒号切分 (原来按最后一个, 密码含冒号会错位)
#  [高]   非 GHA 环境变量写入 ./proxy.env (原来 export 在子 shell 里, exit 后即丢失)
#  [中]   apt-get install base64 -> coreutils (Debian 系无 base64 独立包); sudo 按需使用
#  [中]   端口校验: 纯数字/去前导零/范围 1-65535
#  [中]   userinfo(密码/auth) 做 %xx 解码 (tuic/socks/hysteria2/trojan/anytls)
#  [优化] sing-box check 先验配置; curl 超时+重试; GitHub API 支持 GH_TOKEN; 导出 PROXY_HTTP_SERVER
#  [新增] ss:// (Shadowsocks): SIP002 / 老式整体base64 / 明文兼容，支持 plugin 参数
export LC_ALL=C
set -e

export NODE_LINK=${NODE_LINK:-''}

# GHA 环境写 $GITHUB_ENV；本地环境写入 ./proxy.env（source 加载），避免子 shell export 丢失
if [ -z "$GITHUB_ENV" ]; then
  : > ./proxy.env
fi
set_env() {
  local key=$1
  local value=$2
  if [ -n "$GITHUB_ENV" ]; then
    printf '%s=%s\n' "$key" "$value" >> "$GITHUB_ENV"
  else
    printf '%s=%q\n' "$key" "$value" >> ./proxy.env
  fi
}
proxy_env_hint() {
  if [ -z "$GITHUB_ENV" ] && [ -s ./proxy.env ]; then
    echo "[INFO] 非 GHA 环境：代理变量已写入 ./proxy.env，执行 'source ./proxy.env' 加载"
  fi
}
trap proxy_env_hint EXIT

# 统一 curl：超时 + 重试，避免长时间挂起
_curl() { curl -sL --connect-timeout 10 --max-time 120 --retry 2 --retry-delay 3 "$@"; }

if [ -z "$NODE_LINK" ]; then
  echo "[INFO] 未配置代理，直连模式"
  set_env "IS_PROXY" "false"
  set_env "USE_PROXY" "false"
  set_env "PROXY_STATUS" "直连"
  exit 0
fi

# 智能检测包管理器安装必需依赖
_SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  _SUDO="sudo"
fi
for pkg in jq curl base64; do
  if ! command -v "$pkg" >/dev/null 2>&1; then
    # base64 由 coreutils 提供，各发行版均无独立的 base64 包
    realpkg="$pkg"
    [ "$pkg" = "base64" ] && realpkg="coreutils"
    echo "[WARN] $pkg 未安装，正在尝试安装 $realpkg ..."
    if command -v apt-get >/dev/null 2>&1; then
      $_SUDO apt-get update -q && $_SUDO apt-get install -y "$realpkg"
    elif command -v apk >/dev/null 2>&1; then
      $_SUDO apk add "$realpkg"
    elif command -v yum >/dev/null 2>&1; then
      $_SUDO yum install -y "$realpkg"
    else
      echo "[ERROR] 找不到支持的包管理器安装 $realpkg，请手动安装后重试。"
      exit 1
    fi
  fi
done

echo "[INFO] 获取 sing-box 最新版本..."
_api_hdr=()
_gh_tok="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
[ -n "$_gh_tok" ] && _api_hdr=(-H "Authorization: Bearer $_gh_tok")
tag_name=$(_curl "${_api_hdr[@]}" "https://api.github.com/repos/SagerNet/sing-box/releases/latest" | jq -r '.tag_name // ""' 2>/dev/null || echo "")
latest_version="${tag_name#v}"
if [ -z "$latest_version" ]; then
  echo "[WARN] 无法获取 sing-box 最新版本(可能触发 API 限制)，将默认使用 1.13.14"
  latest_version="1.13.14"
fi
echo "[INFO] 最新稳定版本: v${latest_version}"

ARCH_RAW=$(uname -m)
case "${ARCH_RAW}" in
    'x86_64' | 'amd64')  ARCH='amd64' ;;
    'x86' | 'i686' | 'i386') ARCH='386' ;;
    'aarch64' | 'arm64') ARCH='arm64' ;;
    'armv7l')  ARCH='armv7' ;;
    's390x')   ARCH='s390x' ;;
    *) echo "[ERROR] 不支持的架构: ${ARCH_RAW}"; exit 1 ;;
esac

if [ ! -f "./sing-box" ]; then
  echo "[INFO] 正在下载 sing-box 二进制文件..."
  _curl -o "sing-box-${latest_version}-linux-${ARCH}.tar.gz" "https://github.com/SagerNet/sing-box/releases/download/v${latest_version}/sing-box-${latest_version}-linux-${ARCH}.tar.gz" || true
  if [ -f "sing-box-${latest_version}-linux-${ARCH}.tar.gz" ]; then
    tar -xzf "sing-box-${latest_version}-linux-${ARCH}.tar.gz" 2>/dev/null || true
    if [ -f "sing-box-${latest_version}-linux-${ARCH}/sing-box" ]; then
      mv "sing-box-${latest_version}-linux-${ARCH}/sing-box" ./
    fi
    rm -rf "sing-box-${latest_version}-linux-${ARCH}.tar.gz" "sing-box-${latest_version}-linux-${ARCH}" 2>/dev/null || true
  fi
  if [ ! -f "./sing-box" ]; then echo "[ERROR] sing-box 下载或解压失败！"; exit 1; fi
  chmod +x sing-box
fi
# 已存在的二进制也确保可执行
[ -f ./sing-box ] && chmod +x ./sing-box

# ----------------- 核心安全助手函数 -----------------

# JSON 安全转义函数 (彻底杜绝 JSON 注入崩溃)
json_esc() {
  [ -z "$1" ] && return
  jq -n -c --arg str "$1" '$str' | sed 's/^"//;s/"$//'
}

# 终极安全 URL 解码 (修复原生反斜杠被吞咽的问题)
url_decode() {
  local encoded="${1//+/ }"
  encoded="${encoded//\\/\\\\}"
  printf '%b' "${encoded//%/\\x}"
}

# userinfo(%xx)解码：与 url_decode 不同，userinfo 中的 '+' 是字面加号，不转空格
url_decode_userinfo() {
  local s="${1//\\/\\\\}"
  printf '%b' "${s//%/\\x}" 2>/dev/null || echo ""
}

# 容错 Base64 解码 (支持 URL-Safe 且自动补齐 =)
safe_base64_decode() {
  local input="$1"
  input=$(echo "$input" | tr -d '[:space:]' | tr -- '-_' '+/')
  local mod=$(( ${#input} % 4 ))
  if [ $mod -eq 2 ]; then input="${input}=="; elif [ $mod -eq 3 ]; then input="${input}="; fi
  echo "$input" | base64 -d 2>/dev/null || echo ""
}

# 安全参数提取 (原值)；-m1 防同 key 多次匹配输出多行
get_query_param() {
  local query="$1"
  local key="$2"
  echo "&${query}" | grep -iom1 "&${key}=[^&]*" | cut -d= -f2- || true
}

# 安全参数提取 (强制小写)
get_query_param_lc() {
  get_query_param "$1" "$2" | tr '[:upper:]' '[:lower:]'
}

# 智能解析 Host 和 Port (修复裸 IPv6 冒号切割 Bug)
parse_host_port() {
  local input="$1"
  local default_port="$2"

  # 兼容 NekoBox 导出的 TUIC URI：host:port/?query
  # authority 末尾允许有一个可选的 "/"，解析前去掉，避免端口变成 "30037/"。
  input="${input%/}"
  
  if [[ "$input" =~ ^\[([a-fA-F0-9:]+)\]:([0-9]+)$ ]]; then
    outbound_server="${BASH_REMATCH[1]}"
    outbound_port="${BASH_REMATCH[2]}"
  elif [[ "$input" =~ ^\[([a-fA-F0-9:]+)\]$ ]]; then
    outbound_server="${BASH_REMATCH[1]}"
    outbound_port="$default_port"
  elif [[ "$input" == *":"* ]]; then
    local colons="${input//[^:]/}"
    if [ "${#colons}" -gt 1 ]; then
      outbound_server="$input"
      outbound_port="$default_port"
    else
      outbound_server="${input%:*}"
      outbound_port="${input##*:}"
    fi
  else
    outbound_server="$input"
    outbound_port="$default_port"
  fi
}

# 停止当前 sing-box（4 处重复逻辑收敛为一个函数，统一清理）
stop_current() {
  if [ -n "$CURRENT_SB_PID" ]; then
    kill "$CURRENT_SB_PID" 2>/dev/null || true
    wait "$CURRENT_SB_PID" 2>/dev/null || true
    CURRENT_SB_PID=""
  fi
}

# ----------------- 节点与订阅解析 -----------------

# 智能识别并解码 Base64 整体订阅链接
if ! echo "$NODE_LINK" | grep -q "://"; then
  echo "[INFO] 未检测到明文协议，尝试解析为 Base64 订阅..."
  decoded_sub=$(safe_base64_decode "$NODE_LINK")
  if echo "$decoded_sub" | grep -q "://"; then
    NODE_LINK="$decoded_sub"
    echo "[INFO] ✅ 成功解码 Base64 订阅链接"
  fi
fi

# 兼容 macOS/旧版 Bash 的行数组读取方式 (取代 mapfile)
NODE_ARRAY=()
while IFS= read -r line || [ -n "$line" ]; do
  line=$(echo "$line" | tr -d '[:space:]')
  [ -n "$line" ] && NODE_ARRAY+=("$line")
done <<< "$NODE_LINK"

total_nodes=${#NODE_ARRAY[@]}
echo "[INFO] 共检测到 ${total_nodes} 个代理节点配置行，准备轮询测试..."

node_idx=0
CURRENT_SB_PID=""

for single_node in "${NODE_ARRAY[@]}"; do
  node_idx=$((node_idx + 1))
  echo "----------------------------------------"
  echo "[INFO] 正在尝试节点 [$node_idx / $total_nodes] ..."

  proto=$(echo "$single_node" | cut -d':' -f1 | tr '[:upper:]' '[:lower:]')
  content="${single_node#*://}"
  content="${content%%#*}" # 剔除 #节点名称 尾巴

  # 重置变量
  outbound_type=""
  outbound_server=""
  outbound_port=""
  outbound_uuid=""
  outbound_flow=""
  outbound_transport_type="tcp"
  outbound_path="/"
  outbound_host=""
  outbound_security="none"
  outbound_sni=""
  outbound_fingerprint="chrome"
  outbound_reality_pbk=""
  outbound_reality_sid=""
  outbound_password=""
  outbound_up_mbps=100
  outbound_down_mbps=100
  outbound_obfs_password=""
  outbound_obfs_type="salamander"
  outbound_auth=""
  outbound_congestion="bbr"
  outbound_udp_over_stream="false"
  outbound_udp_relay_mode="native"
  outbound_zerortt="false"
  outbound_username=""
  outbound_password2=""
  outbound_version="5"
  outbound_method=""
  outbound_plugin=""
  outbound_plugin_opts=""
  
  # 默认允许不安全证书（避免 SNI 伪装导致的报错）；如需严格校验可导出 SB_INSECURE=false
  # 注意：insecure=true 会失去对服务端证书的校验，存在中间人风险，请自行权衡
  outbound_insecure="true"
  [ "$(printf '%s' "${SB_INSECURE:-true}" | tr '[:upper:]' '[:lower:]')" = "false" ] && outbound_insecure="false"
  
  outbound_alpn=""

  case "$proto" in
    vless)
      uuid_host="$content"
      uuid="${uuid_host%%@*}"
      rest="${uuid_host#*@}"
      if [[ "$rest" == *"?"* ]]; then host_port="${rest%%"?"*}"; query="${rest#*"?"}"; else host_port="$rest"; query=""; fi
      
      parse_host_port "$host_port" "443"
      outbound_uuid="$uuid"
      outbound_type="vless"

      if [ -n "$query" ]; then
        flow=$(get_query_param "$query" "flow"); [ -n "$flow" ] && outbound_flow="$flow"
        ttype=$(get_query_param_lc "$query" "type"); [ -n "$ttype" ] && outbound_transport_type="$ttype"
        path_raw=$(get_query_param "$query" "path")
        if [ -n "$path_raw" ]; then path_decoded=$(url_decode "$path_raw"); outbound_path="${path_decoded%%"?"*}"; fi
        host=$(get_query_param "$query" "host"); [ -n "$host" ] && outbound_host="$host"
        sec=$(get_query_param_lc "$query" "security"); [ -n "$sec" ] && outbound_security="$sec"
        sni=$(get_query_param "$query" "sni"); [ -n "$sni" ] && outbound_sni="$sni"
        fp=$(get_query_param_lc "$query" "fp"); [ -n "$fp" ] && outbound_fingerprint="$fp"
        pbk=$(get_query_param "$query" "pbk"); [ -n "$pbk" ] && outbound_reality_pbk="$pbk"
        sid=$(get_query_param "$query" "sid"); [ -n "$sid" ] && outbound_reality_sid="$sid"
      fi
      if [ -z "$outbound_host" ]; then outbound_host="$outbound_server"; fi
      if [ -z "$outbound_sni" ]; then outbound_sni="$outbound_server"; fi
      ;;

    vmess)
      decoded=$(safe_base64_decode "$content")
      if [ -z "$decoded" ]; then echo "[WARN] VMess Base64 解码失败"; continue; fi
      
      add=$(echo "$decoded" | jq -r '.add // ""' 2>/dev/null || echo "")
      if [ -z "$add" ]; then echo "[WARN] VMess JSON 解析失败"; continue; fi
      
      outbound_type="vmess"
      outbound_server="$add"
      outbound_port=$(echo "$decoded" | jq -r '.port // 443' 2>/dev/null || echo "443")
      outbound_uuid=$(echo "$decoded" | jq -r '.id // ""' 2>/dev/null || echo "")
      outbound_transport_type=$(echo "$decoded" | jq -r '.net // "tcp"' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "tcp")
      outbound_security=$(echo "$decoded" | jq -r '.tls // ""' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")
      outbound_sni=$(echo "$decoded" | jq -r '.sni // ""' 2>/dev/null || echo "")
      host_raw=$(echo "$decoded" | jq -r '.host // ""' 2>/dev/null || echo "")
      
      path_raw=$(echo "$decoded" | jq -r '.path // "/"' 2>/dev/null || echo "/")
      path_decoded=$(url_decode "$path_raw")
      outbound_path="${path_decoded%%"?"*}"
      
      outbound_host="${host_raw:-$add}"
      outbound_sni="${outbound_sni:-$add}"
      outbound_fingerprint=$(echo "$decoded" | jq -r '.fp // "chrome"' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "chrome")
      ;;

    trojan)
      pass_rest="$content"
      password="${pass_rest%%@*}"
      password="$(url_decode_userinfo "$password")"
      rest="${pass_rest#*@}"
      if [[ "$rest" == *"?"* ]]; then host_port="${rest%%"?"*}"; query="${rest#*"?"}"; else host_port="$rest"; query=""; fi
      
      parse_host_port "$host_port" "443"
      outbound_password="$password"
      outbound_type="trojan"

      if [ -n "$query" ]; then
        ttype=$(get_query_param_lc "$query" "type"); [ -n "$ttype" ] && outbound_transport_type="$ttype"
        path_raw=$(get_query_param "$query" "path")
        if [ -n "$path_raw" ]; then path_decoded=$(url_decode "$path_raw"); outbound_path="${path_decoded%%"?"*}"; fi
        host=$(get_query_param "$query" "host"); [ -n "$host" ] && outbound_host="$host"
        sni=$(get_query_param "$query" "sni"); [ -n "$sni" ] && outbound_sni="$sni"
        fp=$(get_query_param_lc "$query" "fp"); [ -n "$fp" ] && outbound_fingerprint="$fp"
      fi
      if [ -z "$outbound_host" ]; then outbound_host="$outbound_server"; fi
      if [ -z "$outbound_sni" ]; then outbound_sni="$outbound_server"; fi
      ;;

    hysteria2|hy2)
      if [[ "$content" == *"@"* ]]; then auth="${content%%@*}"; host_port="${content#*@}"; else auth=""; host_port="$content"; fi
      auth="$(url_decode_userinfo "$auth")"
      if [[ "$host_port" == *"?"* ]]; then hp="${host_port%%"?"*}"; query="${host_port#*"?"}"; else hp="$host_port"; query=""; fi
      hp="${hp%/}"
      
      parse_host_port "$hp" "443"
      outbound_type="hysteria2"
      outbound_auth="$auth"

      if [ -n "$query" ]; then
        # obfs=混淆类型，obfs-password=混淆密码（两者不要混淆）
        obfs_type=$(get_query_param "$query" "obfs")
        obfs_pw=$(get_query_param "$query" "obfs-password")
        if [ -n "$obfs_pw" ]; then
          outbound_obfs_type="${obfs_type:-salamander}"
          outbound_obfs_password="$(url_decode_userinfo "$obfs_pw")"
        elif [ -n "$obfs_type" ]; then
          # 兼容旧写法：obfs 直接携带密码
          outbound_obfs_type="salamander"
          outbound_obfs_password="$(url_decode_userinfo "$obfs_type")"
        fi
        sni=$(get_query_param "$query" "sni"); [ -n "$sni" ] && outbound_sni="$sni"
        fp=$(get_query_param_lc "$query" "fp"); [ -n "$fp" ] && outbound_fingerprint="$fp"
        upmbps=$(get_query_param "$query" "upmbps"); [[ "$upmbps" =~ ^[0-9]+$ ]] && outbound_up_mbps="$upmbps"
        downmbps=$(get_query_param "$query" "downmbps"); [[ "$downmbps" =~ ^[0-9]+$ ]] && outbound_down_mbps="$downmbps"
        
        if [ -z "$outbound_auth" ]; then
          q_pass=$(get_query_param "$query" "password")
          if [ -z "$q_pass" ]; then q_pass=$(get_query_param "$query" "auth"); fi
          if [ -n "$q_pass" ]; then outbound_auth="$(url_decode_userinfo "$q_pass")"; fi
        fi
      fi
      if [ -z "$outbound_sni" ]; then outbound_sni="$outbound_server"; fi
      ;;

    tuic)
      uuid_pass="${content%%@*}"
      rest="${content#*@}"
      # userinfo 整体先做 %xx 解码，再按第一个冒号切分 uuid:password
      uuid_pass_clean="$(url_decode_userinfo "$uuid_pass")"
      if [[ "$uuid_pass_clean" == *":"* ]]; then outbound_uuid="${uuid_pass_clean%%:*}"; outbound_password2="${uuid_pass_clean#*:}"; else outbound_uuid="$uuid_pass_clean"; outbound_password2=""; fi
      if [[ "$rest" == *"?"* ]]; then host_port="${rest%%"?"*}"; query="${rest#*"?"}"; else host_port="$rest"; query=""; fi
      
      parse_host_port "$host_port" "8443"
      outbound_type="tuic"

      if [ -n "$query" ]; then
        sni=$(get_query_param "$query" "sni"); [ -n "$sni" ] && outbound_sni="$(url_decode "$sni")"
        fp=$(get_query_param_lc "$query" "fp"); [ -n "$fp" ] && outbound_fingerprint="$(url_decode "$fp")"
        cc=$(get_query_param_lc "$query" "congestion_control"); [ -n "$cc" ] && outbound_congestion="$(url_decode "$cc")"
        alpn=$(get_query_param_lc "$query" "alpn"); [ -n "$alpn" ] && outbound_alpn="$(url_decode "$alpn")"

        relay=$(get_query_param_lc "$query" "udp_relay_mode")
        if [ -n "$relay" ]; then
          relay="$(url_decode "$relay")"
          case "$relay" in
            native|quic) outbound_udp_relay_mode="$relay" ;;
          esac
        fi

        uos=$(get_query_param_lc "$query" "udp_over_stream")
        if [ -n "$uos" ]; then
          case "$(url_decode "$uos")" in
            1|true|yes) outbound_udp_over_stream="true" ;;
            0|false|no) outbound_udp_over_stream="false" ;;
          esac
        fi

        zrt=$(get_query_param_lc "$query" "zero_rtt_handshake")
        if [ -n "$zrt" ]; then
          case "$(url_decode "$zrt")" in
            1|true|yes) outbound_zerortt="true" ;;
            0|false|no) outbound_zerortt="false" ;;
          esac
        fi

        ai=$(get_query_param_lc "$query" "allow_insecure")
        if [ -n "$ai" ]; then
          case "$(url_decode "$ai")" in
            1|true|yes) outbound_insecure="true" ;;
            0|false|no) outbound_insecure="false" ;;
          esac
        fi
      fi
      if [ -z "$outbound_sni" ]; then outbound_sni="$outbound_server"; fi
      ;;

    anytls)
      password="${content%%@*}"
      password="$(url_decode_userinfo "$password")"
      rest="${content#*@}"
      if [[ "$rest" == *"?"* ]]; then host_port="${rest%%"?"*}"; query="${rest#*"?"}"; else host_port="$rest"; query=""; fi
      
      parse_host_port "$host_port" "443"
      outbound_password="$password"
      outbound_type="anytls"

      if [ -n "$query" ]; then
        sni=$(get_query_param "$query" "sni"); [ -n "$sni" ] && outbound_sni="$sni"
        fp=$(get_query_param_lc "$query" "fp"); [ -n "$fp" ] && outbound_fingerprint="$fp"
      fi
      if [ -z "$outbound_sni" ]; then outbound_sni="$outbound_server"; fi
      ;;

    socks5|socks)
      if [[ "$content" == *"@"* ]]; then
        user_pass="${content%%@*}"
        host_port="${content#*@}"
        # 按第一个冒号切分 user:password，再做 %xx 解码
        if [[ "$user_pass" == *":"* ]]; then
          outbound_username="$(url_decode_userinfo "${user_pass%%:*}")"
          outbound_password2="$(url_decode_userinfo "${user_pass#*:}")"
        else
          outbound_username="$(url_decode_userinfo "$user_pass")"
          outbound_password2=""
        fi
      else
        host_port="$content"
      fi
      parse_host_port "$host_port" "1080"
      outbound_type="socks"
      ;;

    ss)
      # SIP002: ss://base64(method:password)@host:port/?plugin=xxx#name
      # 兼容老格式: ss://base64(method:password@host:port)#name
      # 兼容非标准明文: ss://method:password@host:port
      ss_full="$content"
      if [[ "$ss_full" == *"?"* ]]; then
        ss_main="${ss_full%%"?"*}"; ss_query="${ss_full#*"?"}"
      else
        ss_main="$ss_full"; ss_query=""
      fi
      if [[ "$ss_main" == *"@"* ]]; then
        _ui_b64="${ss_main%%@*}"
        _hp="${ss_main#*@}"
        _hp="${_hp%/}"  # 去掉 host 尾部可能残留的 /（如 host:port/?plugin=...）
        _ui="$(safe_base64_decode "$_ui_b64")"
        if [ -z "$_ui" ] || [[ "$_ui" != *":"* ]]; then
          # base64 解码失败或无冒号，当作明文 method:password 处理
          _ui="$_ui_b64"
        fi
      else
        _dec="$(safe_base64_decode "$ss_main")"
        if [ -z "$_dec" ]; then echo "[WARN] SS Base64 解码失败"; continue; fi
        # 整体 base64：method:password@host:port，按最后一个 @ 切分 host
        _hp="${_dec##*@}"
        _ui="${_dec%@*}"
      fi
      # method:password 按第一个冒号切分
      outbound_method="${_ui%%:*}"
      _ss_pw="${_ui#*:}"
      if [ -z "$outbound_method" ] || [ "$_ui" = "$outbound_method" ]; then
        echo "[WARN] SS 无法解析出 method:password，跳过"
        continue
      fi
      outbound_password="$(url_decode_userinfo "$_ss_pw")"
      parse_host_port "$_hp" "8388"
      outbound_type="shadowsocks"
      # plugin 形如 obfs-local;obfs=http;obfs-host=x -> plugin + plugin_opts
      if [ -n "$ss_query" ]; then
        _plug="$(get_query_param "$ss_query" "plugin")"
        if [ -n "$_plug" ]; then
          _plug="$(url_decode "$_plug")"
          outbound_plugin="${_plug%%;*}"
          if [[ "$_plug" == *";"* ]]; then outbound_plugin_opts="${_plug#*;}"; fi
        fi
      fi
      ;;

    *)
      echo "[WARN] 不支持的协议类型: $proto，跳过"
      continue
      ;;
  esac

  if [ -z "$outbound_server" ] || ! [[ "$outbound_port" =~ ^[0-9]{1,5}$ ]]; then
    echo "[WARN] 无法解析到有效的服务器地址或数字端口，跳过"
    continue
  fi
  # 去除前导零并校验端口范围（避免 "080" 之类生成非法 JSON）
  outbound_port=$((10#$outbound_port))
  if [ "$outbound_port" -lt 1 ] || [ "$outbound_port" -gt 65535 ]; then
    echo "[WARN] 端口超出有效范围 (1-65535)，跳过"
    continue
  fi

  # ----------------- 生成防注入 JSON 配置 -----------------
  
  # 对所有可能包含特俗符号的变量执行彻底转义，杜绝 JSON 注入
  J_SVR=$(json_esc "$outbound_server")
  J_UID=$(json_esc "$outbound_uuid")
  J_PWD=$(json_esc "$outbound_password")
  J_PW2=$(json_esc "$outbound_password2")
  J_PTH=$(json_esc "$outbound_path")
  J_HST=$(json_esc "$outbound_host")
  J_SNI=$(json_esc "$outbound_sni")
  J_ATH=$(json_esc "$outbound_auth")
  J_OBF=$(json_esc "$outbound_obfs_password")
  J_UNM=$(json_esc "$outbound_username")
  J_PBK=$(json_esc "$outbound_reality_pbk")
  J_SID=$(json_esc "$outbound_reality_sid")
  J_MTD=$(json_esc "$outbound_method")
  J_PLG=$(json_esc "$outbound_plugin")
  J_PLO=$(json_esc "$outbound_plugin_opts")
  # 查询参数类（枚举值）也统一转义，杜绝 JSON 注入
  J_TT=$(json_esc "$outbound_transport_type")
  J_FP=$(json_esc "$outbound_fingerprint")
  J_FLW=$(json_esc "$outbound_flow")
  J_CC=$(json_esc "$outbound_congestion")
  J_OBFT=$(json_esc "$outbound_obfs_type")

  jq_outbound="{\"type\":\"$outbound_type\",\"tag\":\"proxy\",\"server\":\"$J_SVR\",\"server_port\":$outbound_port"
  case "$outbound_type" in
    vless)
      jq_outbound="$jq_outbound,\"uuid\":\"$J_UID\""
      if [ -n "$outbound_flow" ]; then jq_outbound="$jq_outbound,\"flow\":\"$J_FLW\""; fi
      if [ "$outbound_transport_type" != "tcp" ]; then
        if [ "$outbound_transport_type" = "grpc" ]; then
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"grpc\",\"service_name\":\"$J_PTH\"}"
        else
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$J_TT\",\"path\":\"$J_PTH\",\"headers\":{\"Host\":\"$J_HST\"}}"
        fi
      fi
      tls_enabled="false"; if [ "$outbound_security" = "tls" ] || [ "$outbound_security" = "reality" ]; then tls_enabled="true"; fi
      tls_json="{\"enabled\":$tls_enabled,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$J_FP\"}"
      if [ "$outbound_security" = "reality" ]; then tls_json="$tls_json,\"reality\":{\"enabled\":true,\"public_key\":\"$J_PBK\",\"short_id\":\"$J_SID\"}"; fi
      tls_json="$tls_json}"
      jq_outbound="$jq_outbound,\"tls\":$tls_json"
      ;;
    vmess)
      jq_outbound="$jq_outbound,\"uuid\":\"$J_UID\",\"security\":\"auto\""
      if [ "$outbound_transport_type" != "tcp" ]; then
        if [ "$outbound_transport_type" = "grpc" ]; then
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"grpc\",\"service_name\":\"$J_PTH\"}"
        else
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$J_TT\",\"path\":\"$J_PTH\",\"headers\":{\"Host\":\"$J_HST\"}}"
        fi
      fi
      tls_enabled="false"; if [ "$outbound_security" = "tls" ]; then tls_enabled="true"; fi
      jq_outbound="$jq_outbound,\"tls\":{\"enabled\":$tls_enabled,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$J_FP\"}}"
      ;;
    trojan)
      jq_outbound="$jq_outbound,\"password\":\"$J_PWD\""
      if [ "$outbound_transport_type" != "tcp" ]; then
        if [ "$outbound_transport_type" = "grpc" ]; then
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"grpc\",\"service_name\":\"$J_PTH\"}"
        else
          jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$J_TT\",\"path\":\"$J_PTH\",\"headers\":{\"Host\":\"$J_HST\"}}"
        fi
      fi
      jq_outbound="$jq_outbound,\"tls\":{\"enabled\":true,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$J_FP\"}}"
      ;;
    hysteria2)
      jq_outbound="$jq_outbound,\"up_mbps\":$outbound_up_mbps,\"down_mbps\":$outbound_down_mbps"
      if [ -n "$outbound_obfs_password" ]; then jq_outbound="$jq_outbound,\"obfs\":{\"type\":\"$J_OBFT\",\"password\":\"$J_OBF\"}"; fi
      if [ -n "$outbound_auth" ]; then jq_outbound="$jq_outbound,\"password\":\"$J_ATH\""; fi
      jq_outbound="$jq_outbound,\"tls\":{\"enabled\":true,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure}"
      ;;
    tuic)
      jq_outbound="$jq_outbound,\"uuid\":\"$J_UID\""
      if [ -n "$outbound_password2" ]; then jq_outbound="$jq_outbound,\"password\":\"$J_PW2\""; fi
      jq_outbound="$jq_outbound,\"congestion_control\":\"$J_CC\""
      if [ "$outbound_udp_over_stream" = "true" ]; then
        jq_outbound="$jq_outbound,\"udp_over_stream\":true"
      else
        jq_outbound="$jq_outbound,\"udp_relay_mode\":\"$outbound_udp_relay_mode\""
      fi
      jq_outbound="$jq_outbound,\"zero_rtt_handshake\":$outbound_zerortt"
      tls_json="{\"enabled\":true,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure"
      if [ -n "$outbound_alpn" ]; then
        # alpn=a,b 需要生成 JSON 数组，而不是一个包含逗号的字符串
        alpn_json="$(printf '%s' "$outbound_alpn" | awk -F',' '{for(i=1;i<=NF;i++){gsub(/^[[:space:]]+|[[:space:]]+$/, "", $i); if($i!=""){if(n++) printf ","; printf "\"%s\"", $i}}}')"
        tls_json="$tls_json,\"alpn\":[$alpn_json]"
      fi
      tls_json="$tls_json}"
      jq_outbound="$jq_outbound,\"tls\":$tls_json"
      ;;
    anytls)
      jq_outbound="$jq_outbound,\"password\":\"$J_PWD\""
      jq_outbound="$jq_outbound,\"tls\":{\"enabled\":true,\"server_name\":\"$J_SNI\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$J_FP\"}}"
      ;;
    socks)
      if [ -n "$outbound_username" ]; then jq_outbound="$jq_outbound,\"username\":\"$J_UNM\""; fi
      if [ -n "$outbound_password2" ]; then jq_outbound="$jq_outbound,\"password\":\"$J_PW2\""; fi
      jq_outbound="$jq_outbound,\"version\":\"$outbound_version\""
      ;;
    shadowsocks)
      jq_outbound="$jq_outbound,\"method\":\"$J_MTD\",\"password\":\"$J_PWD\""
      if [ -n "$outbound_plugin" ]; then
        jq_outbound="$jq_outbound,\"plugin\":\"$J_PLG\""
        if [ -n "$outbound_plugin_opts" ]; then jq_outbound="$jq_outbound,\"plugin_opts\":\"$J_PLO\""; fi
      fi
      ;;
  esac
  jq_outbound="$jq_outbound}"

  cat << EOF > sing-box-config.json
{
  "log": {"level": "warn"},
  "inbounds": [
    {"type": "socks", "tag": "socks-in", "listen": "127.0.0.1", "listen_port": 1080},
    {"type": "http", "tag": "http-in", "listen": "127.0.0.1", "listen_port": 1081}
  ],
  "outbounds": [$jq_outbound]
}
EOF

  # 先校验配置合法性，失败直接跳过（比启动后猜要快、报错更清晰）
  if ! ./sing-box check -c sing-box-config.json > sing-box-check.log 2>&1; then
    echo "[WARN] ❌ sing-box 配置校验失败，跳过该节点"
    tail -n 5 sing-box-check.log
    continue
  fi

  # 进程深度清理
  # 仅停止当前节点的 sing-box，快速切换，不影响其他进程
  stop_current

  ./sing-box run -c sing-box-config.json > sing-box.log 2>&1 &
  CURRENT_SB_PID=$!
  sleep 2

  if ! kill -0 "$CURRENT_SB_PID" 2>/dev/null; then
    echo "[WARN] ❌ sing-box 启动失败 (配置参数校验不通过)，立即切换下一个..."
    if [ -f sing-box.log ]; then tail -n 5 sing-box.log; fi
    stop_current
    continue
  fi

  echo "[INFO] 测试节点连接性..."
  # 用 socks5h：DNS 解析走代理，避免本地 DNS 污染导致误判节点失效
  ip_info=$(_curl -x socks5h://127.0.0.1:1080 --max-time 8 https://ipinfo.io/json || true)
  # ipinfo 偶发限流：备用端点
  if [ -z "$ip_info" ]; then
    ip_info=$(_curl -x socks5h://127.0.0.1:1080 --max-time 8 "https://api.ipify.org?format=json" || true)
  fi

  if [ -n "$ip_info" ] && echo "$ip_info" | jq -e '.ip' > /dev/null 2>&1; then
    ip_addr=$(echo "$ip_info" | jq -r '.ip // "Unknown"' 2>/dev/null || echo "Unknown")
    country=$(echo "$ip_info" | jq -r '.country // "Unknown"' 2>/dev/null || echo "Unknown")

    echo "[INFO] ✅ 节点 [$node_idx] 连接成功！ | 📍 IP: $ip_addr | 🌍 国家: $country"
    
    set_env "IS_PROXY" "true"
    set_env "USE_PROXY" "true"
    set_env "PROXY_SERVER" "socks5://127.0.0.1:1080"
    set_env "PROXY_HTTP_SERVER" "http://127.0.0.1:1081"
    set_env "PROXY_STATUS" "代理: $ip_addr ($country)"
    exit 0
  else
    echo "[WARN] ❌ 节点 [$node_idx] 无法连接或超时，尝试下一个..."
    if [ -s sing-box.log ]; then tail -n 3 sing-box.log; fi

    # 当前节点失败：只停止当前 PID，立即测试下一个节点
    stop_current
  fi
done

echo "[WARN] ❌ 所有配置的代理节点均测试失败，自动切换为直连模式！"

# 所有节点都失败：只停止当前 PID，不影响其他 sing-box 进程
stop_current

# 明确清除代理环境变量，避免后续 app.py 误认为代理仍然启用
set_env "IS_PROXY" "false"
set_env "USE_PROXY" "false"
set_env "PROXY_SERVER" ""
set_env "PROXY_HTTP_SERVER" ""
set_env "PROXY_STATUS" "直连 (代理全部失效)"

echo "[INFO] ✅ 已切换为直连模式，继续执行后续任务"
exit 0
