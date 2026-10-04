#!/bin/bash
# A7Z 蓝牙配对 agent —— FIFO 喂 yes，自动确认 passkey（免手动）
# 用法: setsid bash bt_agent_start.sh </dev/null >/dev/null 2>&1 &
pkill -f 'bluetoothctl' 2>/dev/null
sleep 1
rm -f /tmp/btctl_fifo
mkfifo /tmp/btctl_fifo
bluetoothctl < /tmp/btctl_fifo > /tmp/bt_agent.log 2>&1 &
{
  echo "power on"
  echo "agent NoInputNoOutput"
  echo "default-agent"
  echo "system-alias TeslaUSB-A7Z"
  echo "pairable on"
  echo "discoverable-timeout 0"
  echo "discoverable on"
  echo "pairable on"
  sleep 3
  while true; do echo yes; sleep 2; done
} > /tmp/btctl_fifo &
echo "agent started" > /tmp/bt_agent_started
