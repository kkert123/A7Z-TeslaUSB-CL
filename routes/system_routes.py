import os, json, time, subprocess, threading
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for
from app_state import state

from utils.app_helpers import get_system_stats
from utils.system_info import get_ip_info, get_wifi_info, get_service_status, get_system_uptime
import utils.system_info

import weixin_notifier
import video_service
import config


system_bp = Blueprint('system', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import get_template_context, get_system_stats, get_disk_usage, _update_sentry_count, get_cached_sentry_events, _get_preview_status, _get_teslacam_health, _update_temp_histories, _update_nvme_temp_history, _update_disk_io, WECOM_BOTS, WECOM_CONFIG_PATH
from utils.hardware_stats import get_all_disks


@system_bp.route('/system')
def system_page():
    return render_template('system.html', **get_template_context())


@system_bp.route('/api/system/stats')
def api_system_stats():
    """API: 获取系统统计信息"""
    try:
        return jsonify({
            'success': True,
            'time': datetime.now().strftime("%H:%M:%S"),
            'service': get_service_status(),
            'sys_stats': get_system_stats(),
            'wifi': get_wifi_info(),
            'ip': get_ip_info(),
            'disk_total': get_disk_usage('/'),
            'disk': get_all_disks(),
            'preview_status': _get_preview_status(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 视频同步 API ──


@system_bp.route('/api/system/service', methods=['POST'])
def api_system_service():
    """控制 teslausb-web 服务"""
    data = request.get_json() or {}
    action = data.get('action', '')
    if action not in ('restart', 'stop', 'start'):
        return jsonify({'success': False, 'error': f'无效操作: {action}'}), 400

    actions_cn = {'restart': '重启', 'stop': '停止', 'start': '启动'}
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', action, 'teslausb-web'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return jsonify({'success': True, 'message': f'服务已{actions_cn[action]}'})
        return jsonify({'success': False, 'error': result.stderr or result.stdout})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/system/reboot', methods=['POST'])
def api_system_reboot():
    """重启系统"""
    try:
        subprocess.Popen(['sudo', 'shutdown', '-r', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': '系统将在1分钟后重启'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    """关机"""
    try:
        subprocess.Popen(['sudo', 'shutdown', '-h', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': '系统将在1分钟后关机'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/system/format-teslacam', methods=['POST'])
def api_system_format_teslacam():
    """
    格式化 TeslaCam 分区 (/dev/nvme0n1p2)。

    流程：
    1. 检查分区存在 + 当前模式
    2. Present模式→停止USB Gadget
    3. 卸载 → mkfs.exfat -n TESLACAM
    3b. blkid 获取新 UUID → 更新 /etc/fstab (替换或追加)
    4. mount rw → 创建 TeslaCam/ 目录 → umount
    5. fsck.exfat 验证
    6. 恢复：Present→重启Gadget(带重试), Edit→mount ro+提醒

    注意：此操作会清除 TeslaCam 分区上的所有数据！
    """
    import time

    CAM_DEV = '/dev/nvme0n1p2'
    CAM_MP = '/mnt/teslacam'
    GADGET_SCRIPT = '/opt/radxa_data/usb_gadget_init.sh'
    MODE_FILE = '/tmp/teslausb_mode'
    # 所有需要通过 USB gadget 暴露的分区（stop/start 时需要确保全部释放）
    LUN_DEVICES = ['/dev/nvme0n1p2', '/dev/nvme0n1p3', '/dev/nvme0n1p4', '/dev/nvme0n1p5']

    try:
        # ── 1. 前置检查 ──
        if not os.path.exists(CAM_DEV):
            return jsonify({'success': False, 'error': f'设备 {CAM_DEV} 不存在'}), 400

        # 检查当前模式
        was_present = False
        try:
            with open(MODE_FILE, 'r') as f:
                was_present = (f.read().strip() == 'present')
        except Exception:
            pass

        # 检查分区是否被其他进程使用
        try:
            holders = os.listdir(f'/sys/block/nvme0n1/nvme0n1p2/holders')
            if holders:
                return jsonify({
                    'success': False,
                    'error': f'TeslaCam 分区被占用 ({", ".join(holders)})，请先切换到 Edit 模式',
                }), 400
        except FileNotFoundError:
            pass  # 目录不存在 = 未被 DM/LVM 占用，可继续

        lines = []

        # ── 2. 停止 Gadget（如果在 Present 模式）──
        if was_present:
            lines.append('🔌 正在断开 USB Gadget...')
            try:
                subprocess.run(
                    ['sudo', '-n', 'bash', GADGET_SCRIPT, 'stop'],
                    capture_output=True, text=True, timeout=30,
                )
                time.sleep(1)
                lines.append('  ✅ Gadget 已停止')
            except Exception as e:
                lines.append(f'  ⚠ Gadget 停止异常: {e}')

            # 强制卸载 TeslaCam
            subprocess.run(['sudo', '-n', 'umount', '-l', CAM_MP],
                         capture_output=True, text=True, timeout=5)

        # 如果 Edit 模式，也尝试卸载
        subprocess.run(['sudo', '-n', 'umount', '-l', CAM_MP],
                     capture_output=True, text=True, timeout=5)
        time.sleep(1)

        # ── 3. 格式化 ──
        lines.append('💿 正在格式化 TeslaCam 分区...')
        lines.append(f'  设备: {CAM_DEV}')

        # 先检查分区标记
        label_type = subprocess.run(
            ['sudo', '-n', 'blkid', '-s', 'TYPE', '-o', 'value', CAM_DEV],
            capture_output=True, text=True, timeout=5
        )
        current_type = label_type.stdout.strip()
        if current_type:
            lines.append(f'  当前文件系统: {current_type}')

        # 执行格式化
        result = subprocess.run(
            ['sudo', '-n', 'mkfs.exfat', '-n', 'TESLACAM', CAM_DEV],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or '格式化失败'
            lines.append(f'  ❌ {error_msg}')
            return jsonify({
                'success': False,
                'error': error_msg,
                'output': '\n'.join(lines),
            }), 500

        lines.append('  ✅ 格式化成功')

        # ── 3b. 获取新 UUID 并更新 /etc/fstab ──
        new_uuid = ''
        try:
            uuid_result = subprocess.run(
                ['sudo', '-n', 'blkid', '-s', 'UUID', '-o', 'value', CAM_DEV],
                capture_output=True, text=True, timeout=10,
            )
            new_uuid = uuid_result.stdout.strip()
            if new_uuid:
                # 更新 /etc/fstab：替换 TeslaCam 分区的 UUID
                fstab_path = '/etc/fstab'
                fstab_bak = fstab_path + '.bak.' + datetime.now().strftime('%Y%m%d_%H%M%S')
                subprocess.run(
                    ['sudo', '-n', 'cp', fstab_path, fstab_bak],
                    capture_output=True, text=True, timeout=5,
                )
                with open(fstab_path, 'r') as f:
                    fstab_lines = f.readlines()
                updated = False
                with open(fstab_path + '.new', 'w') as f:
                    for line in fstab_lines:
                        stripped = line.strip()
                        if stripped and not stripped.startswith('#'):
                            parts = stripped.split()
                            # 行格式: UUID=xxx /mnt/teslacam ...
                            if len(parts) >= 2 and parts[1] == CAM_MP and parts[0].startswith('UUID='):
                                old_uuid = parts[0][5:]  # 去掉 "UUID=" 前缀
                                new_line = line.replace('UUID=' + old_uuid, 'UUID=' + new_uuid, 1)
                                f.write(new_line)
                                updated = True
                                continue
                        f.write(line)
                if updated:
                    subprocess.run(
                        ['sudo', '-n', 'mv', fstab_path + '.new', fstab_path],
                        capture_output=True, text=True, timeout=5,
                    )
                    lines.append(f'📝 已更新 /etc/fstab: UUID={new_uuid} (备份: {os.path.basename(fstab_bak)})')
                else:
                    os.unlink(fstab_path + '.new')
                    # fstab 中找不到 TeslaCam 行，追加新行
                    with open(fstab_path, 'a') as f:
                        new_entry = f'\nUUID={new_uuid} {CAM_MP} exfat defaults,noatime,utf8,umask=000 0 0\n'
                        f.write(new_entry)
                    lines.append(f'📝 已追加 TeslaCam 到 /etc/fstab: UUID={new_uuid} (备份: {os.path.basename(fstab_bak)})')
            else:
                lines.append('⚠ 无法获取新 UUID，请手动更新 /etc/fstab')
        except Exception as e:
            lines.append(f'⚠ 更新 fstab 失败: {e}（不影响格式化，但重启前请手动更新 UUID）')

        # ── 4. 创建 TeslaCam 目录结构（Tesla 需要此目录才能开始录像）──
        lines.append('📁 创建 TeslaCam 目录结构...')
        try:
            # 临时 rw 挂载以创建目录
            subprocess.run(
                ['sudo', '-n', 'mount', '-o', 'rw,noatime', CAM_DEV, CAM_MP],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(0.5)
            teslacam_dir = os.path.join(CAM_MP, 'TeslaCam')
            os.makedirs(teslacam_dir, exist_ok=True)
            lines.append(f'  ✅ 已创建 {teslacam_dir}')
            # 卸载，后续由正常流程重新挂载
            subprocess.run(
                ['sudo', '-n', 'umount', '-l', CAM_MP],
                capture_output=True, text=True, timeout=5,
            )
            time.sleep(0.5)
        except Exception as e:
            lines.append(f'  ⚠ 创建目录失败: {e}（不影响格式化，Tesla 首次连接时会自动创建）')

        # ── 5. 验证 ──
        fsck_result = subprocess.run(
            ['sudo', '-n', 'fsck.exfat', CAM_DEV],
            capture_output=True, text=True, timeout=30,
            input='y\n',  # 自动回答 yes
        )
        lines.append(f'  验证: {fsck_result.stdout.strip() or "OK"}')
        if fsck_result.stderr.strip():
            lines.append(f'  (fsck 备注: {fsck_result.stderr.strip()[:200]})')

        # ── 6. 恢复 Gadget / 挂载分区 ──
        if was_present:
            lines.append('')
            lines.append('🔌 正在恢复 USB Gadget (Present 模式)...')

            # 先确保所有分区已释放（umount -l 可能延迟释放）
            for dev in LUN_DEVICES:
                try:
                    subprocess.run(
                        ['sudo', '-n', 'umount', '-l', dev],
                        capture_output=True, text=True, timeout=5,
                    )
                except Exception:
                    pass
            time.sleep(2)

            gadget_ok = False
            for attempt in (1, 2):
                try:
                    result2 = subprocess.run(
                        ['sudo', '-n', 'bash', GADGET_SCRIPT, 'start'],
                        capture_output=True, text=True, timeout=90,
                    )
                    if result2.returncode == 0:
                        gadget_ok = True
                        lines.append(f'  ✅ Gadget 已恢复 (第 {attempt} 次尝试)')
                        break
                    else:
                        err = (result2.stderr or result2.stdout or '')[:300]
                        lines.append(f'  ⚠ 第 {attempt} 次尝试失败: {err}')
                        if attempt == 1:
                            # 重试前再次卸载所有分区
                            for dev in LUN_DEVICES:
                                subprocess.run(
                                    ['sudo', '-n', 'umount', '-l', dev],
                                    capture_output=True, text=True, timeout=5,
                                )
                            time.sleep(3)
                except subprocess.TimeoutExpired:
                    lines.append(f'  ⚠ 第 {attempt} 次尝试超时')
                except Exception as e:
                    lines.append(f'  ⚠ 第 {attempt} 次异常: {e}')

            if not gadget_ok:
                lines.append('')
                lines.append('❌ USB Gadget 恢复失败，Tesla 可能无法识别设备。')
                lines.append('   请尝试：1) 切到 Edit → 再切回 Present  2) 手动执行:')
                lines.append(f'   sudo bash {GADGET_SCRIPT} restart')
                lines.append(f'   sudo bash {GADGET_SCRIPT} status')
        else:
            # Edit 模式：手动挂载
            subprocess.run(
                ['sudo', '-n', 'mount', '-o', 'ro,noatime', CAM_DEV, CAM_MP],
                capture_output=True, text=True, timeout=10,
            )
            lines.append('📌 已挂载 TeslaCam (只读)')
            lines.append('⚠️  当前为 Edit 模式，请切换至 Present 模式后 Tesla 才能识别')

        return jsonify({
            'success': True,
            'message': 'TeslaCam 分区格式化完成',
            'output': '\n'.join(lines),
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/system/wecom-test', methods=['POST'])
def api_system_wecom_test():
    """测试企业微信推送（可通过 bot 参数指定机器人，默认系统通知）"""
    try:
        data = request.get_json(silent=True) or {}
        bot_key = data.get('bot', 'status')
        base = {b['bot_key']: b['config_key'] for b in WECOM_BOTS}.get(bot_key)
        if not base:
            return jsonify({'success': False, 'error': '无效的机器人'}), 400

        cfg = {}
        if os.path.exists(WECOM_CONFIG_PATH):
            with open(WECOM_CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        key = cfg.get(base) or ''
        if not key:
            return jsonify({'success': False, 'error': '该机器人未启用或未配置 KEY'})

        import urllib.request
        name = {b['bot_key']: b['name'] for b in WECOM_BOTS}.get(bot_key, bot_key)
        url = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}'
        payload = json.dumps({
            'msgtype': 'text',
            'text': {'content': f'🧪 TeslaUSB A7Z 测试推送\n机器人: {name}\nWeb 管理界面测试消息发送成功！'}
        }).encode('utf-8')
        req = urllib.request.Request(url, data=payload,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8', 'ignore')
        try:
            rj = json.loads(body)
            if rj.get('errcode') not in (0, None):
                return jsonify({'success': False,
                                'error': f"企业微信返回错误: {rj.get('errmsg', body)}"})
        except Exception:
            pass
        return jsonify({'success': True, 'message': f'测试推送已发送（{name}），请查看企业微信'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



@system_bp.route('/api/system/wecom-config', methods=['POST'])
def api_system_wecom_config():
    """修改企业微信机器人配置（KEY / 启用开关）

    enabled=False 时把 key 从主字段 {base} 挪到 {base}_disabled，使所有
    cfg.get(base) 的推送点自动失效（禁用真正生效）；enabled=True 写回主字段。
    可逆，无需改动任何推送代码。
    """
    import re
    import tempfile
    try:
        data = request.get_json(silent=True) or {}
        bot_key = data.get('bot')
        base = {b['bot_key']: b['config_key'] for b in WECOM_BOTS}.get(bot_key)
        if not base:
            return jsonify({'success': False, 'error': '无效的机器人'}), 400

        new_key = (data.get('key') or '').strip()
        enabled = bool(data.get('enabled', True))

        cfg = {}
        if os.path.exists(WECOM_CONFIG_PATH):
            with open(WECOM_CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)

        disabled_field = base + '_disabled'
        current_key = cfg.get(base) or cfg.get(disabled_field) or ''
        # 提供了新 key 则用新的，否则沿用现有（只改开关时不覆盖 key）
        effective_key = new_key if new_key else current_key

        if not effective_key:
            return jsonify({'success': False, 'error': '请先填写 KEY'}), 400
        # 企业微信 webhook key 为 UUID 风格，做宽松校验
        if not re.fullmatch(r'[0-9a-fA-F-]{20,64}', effective_key):
            return jsonify({'success': False, 'error': 'KEY 格式无效（应为企业微信 webhook key）'}), 400

        if enabled:
            cfg[base] = effective_key
            cfg.pop(disabled_field, None)
        else:
            cfg[disabled_field] = effective_key
            cfg.pop(base, None)

        # 原子写，避免写入中断损坏配置
        cfg_dir = os.path.dirname(WECOM_CONFIG_PATH)
        fd, tmp = tempfile.mkstemp(dir=cfg_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, WECOM_CONFIG_PATH)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

        name = {b['bot_key']: b['name'] for b in WECOM_BOTS}.get(bot_key, bot_key)
        state = '已启用' if enabled else '已禁用'
        return jsonify({'success': True, 'message': f'{name} 配置已保存（{state}）'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 系统信息 API ──────────────────────────────────────────────


@system_bp.route('/api/system/info')
def api_system_info():
    """系统信息：发行版、内核、架构、CPU 型号、Python 版本"""
    try:
        info = {
            'distro': 'Unknown',
            'kernel': '',
            'arch': '',
            'cpu_model': '',
            'python_version': '',
            'hostname': ''
        }

        # 发行版
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        info['distro'] = line.split('=', 1)[1].strip().strip('"')
                        break
        except:
            pass

        # 内核版本
        try:
            with open('/proc/version', 'r') as f:
                info['kernel'] = f.read().split('(')[0].strip()
        except:
            pass

        # 架构
        import platform
        info['arch'] = platform.machine()

        # CPU 型号
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
            # 尝试 x86: model name
            for line in cpuinfo.split('\n'):
                if line.startswith('model name') and ':' in line:
                    info['cpu_model'] = line.split(':', 1)[1].strip()
                    break
                if line.startswith('Model') and ':' in line:
                    info['cpu_model'] = line.split(':', 1)[1].strip()
                    break
            # ARM 回退：从 implementer/part 识别，支持 big.LITTLE
            if not info['cpu_model']:
                arm_parts = {
                    '0xc07': 'Cortex-A7', '0xc08': 'Cortex-A8', '0xc09': 'Cortex-A9',
                    '0xc0f': 'Cortex-A15', '0xc0e': 'Cortex-A17',
                    '0xd03': 'Cortex-A53', '0xd04': 'Cortex-A35',
                    '0xd05': 'Cortex-A55', '0xd07': 'Cortex-A57',
                    '0xd08': 'Cortex-A72', '0xd09': 'Cortex-A73',
                    '0xd0a': 'Cortex-A75', '0xd0b': 'Cortex-A76',
                    '0xd0c': 'Cortex-A77', '0xd0d': 'Cortex-A78',
                    '0xd41': 'Cortex-A78AE', '0xd44': 'Cortex-X1',
                    '0xd46': 'Cortex-A510', '0xd47': 'Cortex-A710',
                    '0xd48': 'Cortex-X2', '0xd49': 'Cortex-A520',
                    '0xd4a': 'Cortex-A720', '0xd4b': 'Cortex-X925',
                }
                import collections
                core_counts = collections.Counter()
                arch_val = '8'
                for line in cpuinfo.split('\n'):
                    if line.startswith('CPU part') and ':' in line:
                        p = line.split(':', 1)[1].strip()
                        core_name = arm_parts.get(p, f'ARM-0x{p}')
                        core_counts[core_name] += 1
                    if line.startswith('CPU architecture') and ':' in line:
                        arch_val = line.split(':', 1)[1].strip()

                arch_str = f'ARMv{int(arch_val, 16) if arch_val else "?"}-A'
                if len(core_counts) == 1:
                    name, count = list(core_counts.items())[0]
                    info['cpu_model'] = f'{arch_str} {name} ({count} cores)'
                elif len(core_counts) > 1:
                    parts = []
                    for name, count in core_counts.items():
                        parts.append(f'{name} x{count}')
                    info['cpu_model'] = f'{arch_str} {" + ".join(parts)}'
        except:
            pass

        # Python 版本
        import sys
        info['python_version'] = sys.version.split()[0]
        info['python'] = info['python_version']  # 兼容前端 JS 字段名

        # 主机名
        import socket
        try:
            info['hostname'] = socket.gethostname()
        except:
            pass

        # 系统运行时间
        info['uptime'] = get_system_uptime()

        response = {'success': True, **info}
        return jsonify(response)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# ── 版本管理 API ──────────────────────────────────────────────

@system_bp.route('/api/version/check')
def api_version_check():
    """查询当前版本与 GitHub 最新 release"""
    try:
        import version_service
        result = version_service.check_latest_release()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/version/token', methods=['GET', 'POST'])
def api_version_token():
    """读取/保存 GitHub 版本检测令牌（写入 config/sentry.json）"""
    cfg_path = getattr(config, 'SENTRY_CONFIG_FILE', '') or '/opt/radxa_data/teslausb/config/sentry.json'
    if request.method == 'GET':
        token = ''
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    token = json.load(f).get('github_version_token', '')
            except (json.JSONDecodeError, IOError):
                pass
        return jsonify({
            'success': True,
            'has_token': bool(token),
            'token_preview': (token[:8] + '***') if len(token) > 8 else ''
        })

    data = request.get_json(silent=True) or {}
    new_token = (data.get('token') or '').strip()
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    if new_token:
        cfg['github_version_token'] = new_token
    else:
        cfg.pop('github_version_token', None)

    import tempfile
    cfg_dir = os.path.dirname(cfg_path)
    os.makedirs(cfg_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cfg_dir, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cfg_path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        return jsonify({'success': False, 'error': '配置文件写入失败'}), 500

    return jsonify({
        'success': True,
        'has_token': bool(new_token),
        'message': '令牌已保存' if new_token else '令牌已清除'
    })


@system_bp.route('/api/version/upgrade', methods=['POST'])
def api_version_upgrade():
    """触发一键升级（同步执行，前端需等待）"""
    data = request.get_json(silent=True) or {}
    new_version = (data.get('version') or '').strip()
    asset_url = (data.get('asset_url') or '').strip()
    sha256 = (data.get('sha256') or '').strip()
    sig_url = (data.get('sig_url') or '').strip()

    if not all([new_version, asset_url, sha256]):
        return jsonify({'success': False, 'error': '缺少参数: version/asset_url/sha256 必需'}), 400

    try:
        import upgrade_service
        ok, msg = upgrade_service.do_upgrade(new_version, asset_url, sha256, sig_url or None)
        if ok:
            return jsonify({
                'success': True,
                'message': msg,
                'version': new_version,
                'need_restart': True,
            })
        return jsonify({'success': False, 'error': msg}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/version/rollback-info')
def api_version_rollback_info():
    """查询是否可回退、回退到哪个版本"""
    try:
        import upgrade_service
        info = upgrade_service.get_rollback_info()
        return jsonify({
            'success': True,
            'can_rollback': info is not None,
            'rollback_info': info,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/version/rollback', methods=['POST'])
def api_version_rollback():
    """执行回退"""
    try:
        import upgrade_service
        ok, msg = upgrade_service.do_rollback()
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 系统挂载点 API ──────────────────────────────────────────────

@system_bp.route('/api/system/mounts')
def api_system_mounts():
    """系统挂载点信息（df -h + 缓存回退，支持 Present 模式下显示未挂载分区）"""
    try:
        mounts = []
        # 获取当前已挂载的文件系统
        result = subprocess.run(
            ['df', '-h', '--output=target,source,fstype,size,used,avail,pcent'],
            capture_output=True, text=True, timeout=5
        )
        mounted_paths = set()
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) >= 7:
                        mount_point = parts[0]
                        mounted_paths.add(mount_point)
                        mounts.append({
                            'mount_point': mount_point,
                            'device': parts[1],
                            'fs_type': parts[2],
                            'total_fmt': parts[3],
                            'used_fmt': parts[4],
                            'avail': parts[5],
                            'mounted': True,
                            'mount': mount_point,
                            'source': parts[1],
                            'fstype': parts[2],
                            'size': parts[3],
                            'used': parts[4],
                            'percent': parts[6].rstrip('%')
                        })

        # 从缓存补充未挂载分区（Present 模式下非 teslacam 分区不可见）
        cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    cache = json.load(f)
                for name, info in cache.items():
                    mp = '/mnt/' + name
                    if mp not in mounted_paths and os.path.exists(mp):
                        mounts.append({
                            'mount_point': mp,
                            'device': info.get('device', '—'),
                            'fs_type': info.get('fs_type', info.get('fstype', '—')),
                            'total_fmt': info.get('total_fmt', '—'),
                            'used_fmt': info.get('used_fmt', '—'),
                            'avail': info.get('free_fmt', '—'),
                            'mounted': False,
                            'mount': mp,
                            'source': info.get('device', '—'),
                            'fstype': info.get('fs_type', '—'),
                            'size': info.get('total_fmt', '—'),
                            'used': info.get('used_fmt', '—'),
                            'percent': str(info.get('percent', 0))
                        })
            except:
                pass

        return jsonify({'success': True, 'mounts': mounts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 分析数据 API ──────────────────────────────────────────────

# ── Auto Present API (v92) ────────────────────────────────────

@system_bp.route('/api/auto-present/status')
def api_auto_present_status():
    """获取 Auto Present 状态（倒计时剩余秒数）"""
    try:
        import auto_present_service as aps
        return jsonify({"success": True, **aps.get_status()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@system_bp.route('/api/auto-present/config', methods=['GET', 'POST'])
def api_auto_present_config():
    """获取或更新 Auto Present 配置"""
    try:
        import auto_present_service as aps
        if request.method == 'GET':
            return jsonify({"success": True, **aps.get_status()})
        
        data = request.get_json(silent=True) or {}
        enabled = None
        timeout_minutes = None
        if 'enabled' in data:
            enabled = bool(data['enabled'])
        if 'timeout_minutes' in data:
            try:
                timeout_minutes = int(data['timeout_minutes'])
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "超时时间必须为整数（分钟）"}), 400
        
        config = aps.update_config(enabled=enabled, timeout_minutes=timeout_minutes)
        return jsonify({"success": True, **aps.get_status(), "message": "配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@system_bp.route('/api/auto-present/cancel', methods=['POST'])
def api_auto_present_cancel():
    """手动取消当前倒计时（不切回 Present）"""
    try:
        import auto_present_service as aps
        aps.cancel_countdown()
        return jsonify({"success": True, "message": "倒计时已取消"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 风扇温控曲线 API ──────────────────────────────────────────


FAN_CURVE_FILE = '/opt/radxa_data/teslausb/data/fan_curve.json'
FAN_CURVE_DEFAULT = {
    "curve": [
        {"temp": 55, "pwm": 102},
        {"temp": 60, "pwm": 170},
        {"temp": 65, "pwm": 210},
        {"temp": 70, "pwm": 255},
    ],
    "lowest_pwm": 50,
}


def _load_fan_curve() -> dict:
    """加载风扇温控曲线配置"""
    if os.path.exists(FAN_CURVE_FILE):
        try:
            with open(FAN_CURVE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return dict(FAN_CURVE_DEFAULT)


def _save_fan_curve(config: dict) -> bool:
    """保存风扇温控曲线配置"""
    try:
        os.makedirs(os.path.dirname(FAN_CURVE_FILE), exist_ok=True)
        with open(FAN_CURVE_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except IOError:
        return False


@system_bp.route('/api/fan/curve', methods=['GET', 'POST'])
def api_fan_curve():
    """读取或更新风扇温控曲线"""
    if request.method == 'GET':
        return jsonify({"success": True, "data": _load_fan_curve()})
    data = request.get_json(silent=True) or {}
    curve = data.get('curve')
    lowest = data.get('lowest_pwm')
    if curve is not None:
        # 验证曲线格式
        if not isinstance(curve, list) or len(curve) < 1:
            return jsonify({"success": False, "error": "curve 必须是非空数组"}), 400
        for e in curve:
            if not isinstance(e, dict) or 'temp' not in e or 'pwm' not in e:
                return jsonify({"success": False, "error": "每条曲线需包含 temp 和 pwm"}), 400
            e['temp'] = int(e['temp'])
            e['pwm'] = int(e['pwm'])
        curve.sort(key=lambda x: x['temp'])
    config = _load_fan_curve()
    if curve is not None:
        config['curve'] = curve
    if lowest is not None:
        config['lowest_pwm'] = max(0, min(255, int(lowest)))
    if _save_fan_curve(config):
        return jsonify({"success": True, "message": "已保存", "data": config})
    return jsonify({"success": False, "error": "保存失败"}), 500


# ── 分析数据 API ──────────────────────────────────────────────


@system_bp.route('/api/system/stats-stream')
def api_system_stats_stream():
    """SSE 系统状态实时流（替代 30s 轮询）"""
    import queue
    q = queue.Queue(maxsize=50)
    with state.stats_subscribers_lock:
        state.stats_subscribers.append(q)

    def generate():
        try:
            # 立即发送一次当前状态
            try:
                _update_temp_histories()
                _update_nvme_temp_history()
                _update_disk_io()
                stats = {
                    'time': datetime.now().strftime("%H:%M:%S"),
                    'service': get_service_status(),
                    'sys': (sys_init := get_system_stats()),
                    'wifi': get_wifi_info(),
                    'ip': get_ip_info(),
                    'disk_total': get_disk_usage('/'),
                    'disk': get_all_disks(),
                    'preview_status': _get_preview_status(),
                    'teslacam_health': _get_teslacam_health(),
                }
                stats['nvme_total_disk'] = sys_init.get('nvme_total_disk')
                stats['power_on_hours_fmt'] = sys_init.get('power_on_hours_fmt')
                stats['monthly_traffic'] = sys_init.get('monthly_traffic')
                stats['gpu_npu'] = sys_init.get('gpu_npu')
                _update_sentry_count()
                stats['sentry_events'] = get_cached_sentry_events()
                # USB Gadget 状态
                try:
                    import gadget_health
                    stats['gadget_status'] = gadget_health.get_gadget_status()
                except Exception:
                    stats['gadget_status'] = {'udc_bound': None, 'last_error': 'gadget_health 加载失败'}
                yield f"data: {json.dumps(stats)}\n\n"
            except:
                pass

            while True:
                try:
                    stats = q.get(timeout=30)
                    yield f"data: {json.dumps(stats)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with state.stats_subscribers_lock:
                if q in state.stats_subscribers:
                    state.stats_subscribers.remove(q)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ═══════════════════════════════════════════════════════════════
# 缩略图服务
# ═══════════════════════════════════════════════════════════════

