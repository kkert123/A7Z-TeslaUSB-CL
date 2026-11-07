#!/usr/bin/env bash
# ============================================================
# guardian-b · 自签证书生成（CA + 服务端证书，A 侧 pinning）
# ============================================================
# 用法：
#   ./gen_selfsigned.sh <VPS_IP> [OUTDIR=/opt/guard-b/tls]
#
# 产出：
#   ca.crt        私有 CA 证书  → **拷到 A 侧做 pinning 信任锚**（唯一的跨机物料）
#   ca.key        私有 CA 私钥  → 留在 VPS / 离线保存，切勿外传
#   server.crt    服务端证书（SAN=IP:<VPS_IP>）
#   server.key    服务端私钥（OpenResty 用）
#   fullchain.crt server.crt + ca.crt（OpenResty ssl_certificate 指向它）
#
# 设计依据：docs/explanation/自检守护-B端VPS设计.md §9.1
#   —— 自签 + pinning，零续期、零外部 CA、免疫 CA/B 政策漂移。
#   采用「私有 CA + 服务端证书」两级：日后轮换 server 证书无需改 A（A 只 pin CA）。
#
# 依赖：openssl（Debian/Ubuntu 自带）
set -euo pipefail

IP="${1:-}"
OUTDIR="${2:-/opt/guard-b/tls}"

if [ -z "$IP" ]; then
  echo "用法: $0 <VPS_IP> [OUTDIR]" >&2
  echo "例如: $0 129.211.1.2 /opt/guard-b/tls" >&2
  exit 2
fi

# 简单校验 IPv4/IPv6 形状（够用即可；真正校验交给 openssl）
case "$IP" in
  *[!0-9a-fA-F:.]*) echo "IP 形状可疑: $IP" >&2; exit 2;;
esac

mkdir -p "$OUTDIR"
cd "$OUTDIR"

CA_DAYS=3650     # 私有 CA 10 年
LEAF_DAYS=3650   # 服务端证书 10 年（pinning 场景无 398/200 天上限约束）

echo "==> 1/4 生成私有 CA"
openssl genrsa -out ca.key 4096 2>/dev/null
openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" \
  -subj "/CN=guard-b-local-ca" -out ca.crt

echo "==> 2/4 生成服务端私钥与 CSR"
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -subj "/CN=$IP" -out server.csr

echo "==> 3/4 签发服务端证书（SAN=IP:$IP）"
cat > san.cnf <<EOF
subjectAltName=IP:$IP
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days "$LEAF_DAYS" -sha256 -extfile san.cnf 2>/dev/null

cat server.crt ca.crt > fullchain.crt

echo "==> 4/4 校验与指纹"
openssl verify -CAfile ca.crt server.crt

echo
echo "-------- 物料清单（$OUTDIR）--------"
ls -1 ca.crt ca.key server.crt server.key fullchain.crt server.csr san.cnf
echo
echo "服务端证书 SHA-256 指纹（可与 A 侧核对）:"
openssl x509 -in server.crt -noout -fingerprint -sha256
echo
echo "=== 下一步 ==="
echo "1) OpenResty（deploy/openresty-guard-b.conf）:"
echo "     ssl_certificate     $OUTDIR/fullchain.crt;"
echo "     ssl_certificate_key $OUTDIR/server.key;"
echo "2) 把 **ca.crt** 拷到 A 端（例如 /opt/radxa_data/teslausb/config/guard-b-ca.crt）。"
echo "3) A 端心跳请求用证书固定："
echo "     requests.post(url, verify='config/guard-b-ca.crt', ...)"
echo "   —— 因证书 SAN 是 IP，A 必须用 **IP 直连**（非域名），否则校验不过。"
echo
echo "安全提醒：ca.key 只用于签发；签发完可离线保存。轮换 server 证书时 A 无需改动。"

# 清理临时 CSR/配置（保留证书与私钥）
rm -f server.csr san.cnf ca.srl 2>/dev/null || true
