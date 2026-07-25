#!/usr/bin/env python3
"""风扇温控服务 — 独立于看门狗，每5秒根据温度调整风扇转速"""
import json, os, time

PWM_PATH = None
TEMP_PATH = None
CURVE_FILE = '/opt/radxa_data/teslausb/data/fan_curve.json'

def _find_hwmon():
    """自动检测 PWM 和温度路径"""
    global PWM_PATH, TEMP_PATH
    # PWM 风扇
    for hwmon in range(0, 15):
        path = f'/sys/class/hwmon/hwmon{hwmon}/pwm1'
        if os.path.exists(path):
            PWM_PATH = path
            break
    # CPU 温度
    for tp in ['/sys/class/thermal/thermal_zone0/temp', '/sys/class/hwmon/hwmon0/temp1_input']:
        if os.path.exists(tp):
            TEMP_PATH = tp
            break

def _read_temp():
    try:
        with open(TEMP_PATH, 'r') as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None

def _load_curve():
    defaults = {
        'curve': [
            {'temp': 55, 'pwm': 102},
            {'temp': 60, 'pwm': 170},
            {'temp': 65, 'pwm': 210},
            {'temp': 70, 'pwm': 255},
        ],
        'lowest_pwm': 50,
    }
    try:
        with open(CURVE_FILE, 'r') as f:
            saved = json.load(f)
        if saved.get('curve'):
            return saved
    except Exception:
        pass
    return defaults

def apply_curve():
    """读温度 + 匹配曲线 + 写 PWM"""
    if not PWM_PATH or not TEMP_PATH:
        _find_hwmon()
    if not PWM_PATH or not TEMP_PATH:
        return
    try:
        temp = _read_temp()
        if temp is None:
            return
        curve = _load_curve()
        target = curve.get('lowest_pwm', 50)
        for entry in sorted(curve.get('curve', []), key=lambda e: e['temp']):
            if temp >= entry['temp']:
                target = entry['pwm']
        with open(PWM_PATH, 'w') as f:
            f.write(str(target))
    except Exception:
        pass

if __name__ == '__main__':
    _find_hwmon()
    print(f'[fan_control] PWM={PWM_PATH} TEMP={TEMP_PATH}')
    while True:
        apply_curve()
        time.sleep(5)
