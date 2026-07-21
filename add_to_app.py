"""
# DEPRECATED - DO NOT USE - 已废弃，不要使用
# 此文件的路由已完整实现在 app.py (第740行附近)
# 两个实现的逻辑不一致 (UDC 检测 vs flag 文件)
# 拼接到 app.py 会导致 Flask AssertionError (路由冲突)
# 保留仅作历史参考，所有功能请使用 app.py

--- 原内容 ---
Task #14 添加内容：USB 模式切换 API 端点
将此代码添加到 app.py 的路由定义部分（建议在 /api 路由区域）
"""

import os
import subprocess

# ─────────────────────────────────────────────
# USB 模式切换 API
# ─────────────────────────────────────────────

@app.route('/api/mode/status', methods=['GET'])
@require_auth
def get_mode_status():
    """获取当前 USB 模式（Present/Edit）"""
    try:
        # 检查 USB Gadget 是否激活（Present Mode）
        udc_path = '/sys/kernel/config/usb_gadget/teslausb/UDC'
        is_present = os.path.exists(udc_path) and os.path.getsize(udc_path) > 0
        
        mode = 'present' if is_present else 'edit'
        
        return jsonify({
            'success': True,
            'mode': mode,
            'message': f'当前模式: {"Present (连接 Tesla)" if mode == "present" else "Edit (网络访问)"}'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/mode/switch', methods=['POST'])
@require_auth
def switch_mode():
    """切换 USB 模式（Present <-> Edit）"""
    try:
        data = request.get_json()
        new_mode = data.get('mode', 'present')
        
        if new_mode not in ['present', 'edit']:
            return jsonify({'success': False, 'message': '无效的模式，必须是 present 或 edit'}), 400
        
        # 脚本路径
        script_dir = '/opt/radxa_data'
        script_path = os.path.join(script_dir, 'present_usb.sh' if new_mode == 'present' else 'edit_usb.sh')
        
        if not os.path.exists(script_path):
            return jsonify({'success': False, 'message': f'脚本不存在: {script_path}'}), 404
        
        # 执行切换脚本（超时 30 秒）
        result = subprocess.run(
            ['sudo', script_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            mode_name = 'Present (连接 Tesla)' if new_mode == 'present' else 'Edit (网络访问)'
            return jsonify({
                'success': True,
                'mode': new_mode,
                'message': f'已切换到 {mode_name} 模式',
                'output': result.stdout[-200:] if result.stdout else None  # 只返回最后 200 字符
            })
        else:
            error_msg = result.stderr or result.stdout or '未知错误'
            return jsonify({'success': False, 'message': error_msg}), 500
    
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'message': '切换超时（30秒），请检查设备状态'}), 500
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
