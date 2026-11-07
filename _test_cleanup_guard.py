# -*- coding: utf-8 -*-
"""v0.3.1.63 清理守卫定向测试（本地，不触碰设备）。

覆盖：
  A. _detect_cam_mode() 分支（含最关键的「Edit 模式 ro 挂载必须判成 edit」）
  B. /api/cleanup/execute 在 Present 下必须 409 且【零删除调用】
  D. 失败/部分失败语义 与 单条历史
  R. Edit 模式 ro → remount 成功/失败 两条路径
  E. /api/cleanup/history 裁剪与脏数据健壮性
"""
import os
import sys
import json
import types
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 假的 auto_cleanup 模块（避免依赖设备路径）──────────────────
CALLS = []
STATS = {}


class FakeCleaner(object):
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.stats = STATS['s']

    def cleanup_previews(self):
        CALLS.append('cleanup_previews')
        return STATS.get('nonvideo', 0)

    def cleanup_temp_files(self):
        CALLS.append('cleanup_temp_files')
        return 0

    def cleanup_logs(self):
        CALLS.append('cleanup_logs')
        return 0

    def cleanup_old_versions(self, keep=2):
        CALLS.append('cleanup_old_versions')
        return 0

    def calculate_cleanup_plan(self, *a, **k):
        CALLS.append('calculate_cleanup_plan')
        return STATS['plan']

    def execute_cleanup(self, *a, **k):
        CALLS.append('execute_cleanup')
        vd = STATS.get('video_deleted', STATS.get('deleted', 0))
        # deleted_files 含孤儿辅助文件，可与 breakdown 的视频数不同（G-1 场景）
        STATS['s']['deleted_files'] = STATS.get('total_deleted') or vd
        STATS['s']['errors'] = list(STATS.get('exec_errors', []))
        STATS['s']['missing_files'] = STATS.get('missing', 0)
        STATS['s']['breakdown'] = {'SentryClips': {'deleted': vd, 'freed_bytes': 0,
                                                  'freed_gb': 0}} if vd else {}
        return STATS['s']


fake = types.ModuleType('auto_cleanup')
fake.AutoCleaner = FakeCleaner
fake.PREVIEW_DIR = '/tmp/preview'
fake.DATA_DIR = '/tmp/data'
fake.PREVIEW_MAX_AGE_DAYS = 7
fake.TEMP_MAX_AGE_DAYS = 1
fake.LOG_MAX_AGE_DAYS = 30
fake.DISK_THRESHOLD_WARNING = 85
fake.DISK_THRESHOLD_CRITICAL = 90
fake.DISK_THRESHOLD_EMERGENCY = 95
fake.ALLOWED_FOLDER_NAMES = []
fake.load_policies = lambda: {}
fake.save_policies = lambda p: None
fake.get_global_settings = lambda: {}
fake.save_global_settings = lambda s: None
# M122：路由的历史读写委托给 auto_cleanup 的原子实现，替身必须提供同名接口
import auto_cleanup as _real_ac               # noqa: E402
fake.load_cleanup_history = _real_ac.load_cleanup_history
fake.append_cleanup_history = _real_ac.append_cleanup_history
fake.atomic_write_json = _real_ac.atomic_write_json
fake._HISTORY_STATE = _real_ac._HISTORY_STATE
sys.modules['auto_cleanup'] = fake

from flask import Flask                      # noqa: E402
import utils.system_info as si               # noqa: E402
import routes.cleanup_routes as cr           # noqa: E402

# 桩会覆盖 si.read_cam_mount，故先留存导入时的原始对象用于 A8 身份断言
_ORIG_RCM, _ORIG_DCM = si.read_cam_mount, si.detect_cam_mode
# post_execute 会把 _ensure_cam_writable 桩掉，W 组要用真身
_ORIG_ECW = cr._ensure_cam_writable

FAILS = []
N = [0]


def check(name, cond, extra=''):
    N[0] += 1
    print('%-58s %s %s' % (name, 'PASS' if cond else 'FAIL', extra if not cond else ''))
    if not cond:
        FAILS.append(name)


# 本机没有 /mnt/teslacam，桩掉 isdir（只对含 teslacam 的路径放行）
_real_isdir = os.path.isdir
os.path.isdir = lambda p: True if (isinstance(p, str) and 'teslacam' in p) else _real_isdir(p)


# ── A. 判据分支 ────────────────────────────────────────────────
def detect(mounted, is_ro, source, bound):
    # 判据实现已统一到 utils.system_info（唯一权威实现），直接测它
    si.read_cam_mount = lambda: (mounted, is_ro, source)
    si.gadget_bound = lambda: bound
    return si.detect_cam_mode()[0]


check('A1 Present: 直挂 ro + gadget 已绑 → present',
      detect(True, True, '/dev/nvme0n1p2', True) == 'present')
check('A2 Present: 只读回环 loop0 + gadget 已绑 → present',
      detect(True, True, '/dev/loop0', True) == 'present')
check('A3 **Edit: 直挂 ro + gadget 未绑 → edit（关键回归点）**',
      detect(True, True, '/dev/nvme0n1p2', False) == 'edit')
check('A4 Edit: 直挂 rw + gadget 未绑 → edit',
      detect(True, False, '/dev/nvme0n1p2', False) == 'edit')
check('A5 Present: cam 未挂载但 gadget 已绑 → present',
      detect(False, False, '', True) == 'present')
check('A6 只读回环 + gadget 未绑 → present（无法放开写）',
      detect(True, True, '/dev/loop1', False) == 'present')
check('A7 未挂载且无 gadget → unmounted',
      detect(False, False, '', False) == 'unmounted')
check('A8 cleanup_routes 引用的就是唯一权威实现（无第二份判据）',
      cr._detect_cam_mode is _ORIG_DCM and cr._read_cam_mount is _ORIG_RCM)
_src = open('routes/misc_routes.py', encoding='utf-8').read()
check('A9 mode/status 兜底已改用统一探测且不再硬编码 g1',
      'detect_cam_mode' in _src and '/usb_gadget/g1/UDC' not in _src)
check('A10 system_info 里 Present 判据不含「仅 ro 即 present」写法',
      'elif mounted and is_ro and is_loop_source' in open('utils/system_info.py', encoding='utf-8').read())

# ── 端点行为 ───────────────────────────────────────────────────
tmpd = tempfile.mkdtemp(prefix='cu_')
cr.CLEANUP_LOG = os.path.join(tmpd, 'cleanup_history.json')

app = Flask(__name__)
app.register_blueprint(cr.cleanup_bp)

MOUNTS = []


def _fake_read_mount():
    if len(MOUNTS) > 1:
        return MOUNTS.pop(0)
    return MOUNTS[0]


def _fake_subprocess_run(cmd, **kw):
    class R(object):
        returncode = 0
        stdout = ''
        stderr = ''
    return R()


def post_execute(mode, plan_count=3, deleted=0, missing=0, nonvideo=0,
                 exec_errors=None, mounts=None, total_deleted=None, writable=None):
    del CALLS[:]
    STATS.clear()
    STATS.update({'plan': {'total_count': plan_count, 'total_size_gb': 1.0,
                           'breakdown': {}, 'protected_count': 0},
                  'deleted': deleted, 'missing': missing, 'nonvideo': nonvideo,
                  'total_deleted': total_deleted,
                  'exec_errors': exec_errors or []})
    STATS['s'] = {'errors': [], 'actions': [], 'deleted_files': 0,
                  'freed_bytes': 0, 'skipped_files': 0, 'missing_files': 0,
                  'breakdown': {}}
    MOUNTS[:] = mounts or [(mode != 'unmounted', mode == 'present', '/dev/nvme0n1p2')]
    # 注意：_detect_cam_mode 直接桩掉，避免它预消费掉 MOUNTS 队列
    #（_read_cam_mount 只留给 remount 块使用）
    cr._detect_cam_mode = lambda: (mode, {'mounted': mode != 'unmounted',
                                          'is_ro': mode == 'present',
                                          'source': '/dev/nvme0n1p2',
                                          'gadget_bound': mode == 'present'})
    cr._read_cam_mount = _fake_read_mount
    cr._gadget_bound = lambda: (mode == 'present')
    cr.subprocess.run = _fake_subprocess_run
    # 「确认 cam 可写（remount + 写探针）」单独成组测试；端点组里桩成通过
    cr._ensure_cam_writable = (lambda cam, lines: writable) if writable else (lambda cam, lines: (True, ''))
    with app.test_client() as c:
        r = c.post('/api/cleanup/execute', json={})
    return r.status_code, (r.get_json() or {})


PRESENT_M = [(True, True, '/dev/nvme0n1p2')]
EDIT_RO = [(True, True, '/dev/nvme0n1p2')]
EDIT_RW = [(True, False, '/dev/nvme0n1p2')]

code, body = post_execute('present', mounts=PRESENT_M)
check('B1 Present → HTTP 409', code == 409, 'got %s' % code)
check('B2 Present → success False', body.get('success') is False)
check('B3 Present → 零清理动作（未删除任何文件）', CALLS == [], 'calls=%s' % CALLS)
check('B4 Present → 输出提示切 Edit', 'Edit' in (body.get('output') or ''))
code, body = post_execute('present', nonvideo=999, mounts=PRESENT_M)
check('B5 Present → 连非视频清理也不执行', 'cleanup_previews' not in CALLS, '%s' % CALLS)

# ── W: _ensure_cam_writable（remount + 真实写探针）───────────────────
import tempfile as _tf
import shutil as _sh

_cam = _tf.mkdtemp(prefix='cam_')
_ORIG_RCM2 = cr._read_cam_mount


def ensure(mounted, is_ro, path, mounts_seq=None):
    seq = mounts_seq or [(mounted, is_ro, path)]
    it = list(seq)
    cr._read_cam_mount = (lambda: (it.pop(0) if len(it) > 1 else it[0]))
    lines = []
    ok, why = cr._ensure_cam_writable(path, lines)
    return ok, why, lines


cr.subprocess.run = _fake_subprocess_run
cr._ensure_cam_writable = _ORIG_ECW          # 用真身（前面的端点组把它桩掉了）
ok, why, lines = ensure(True, False, _cam)
check('W1 已 rw 且目录可写 → ok, 写探针通过', ok and any('写探针通过' in l for l in lines), str(why))
check('W2 探针文件已清理（不留痕）',
      not os.path.exists(os.path.join(_cam, '.wb_write_probe')))

ok, why, lines = ensure(True, True, _cam, [(True, True, '/dev/nvme0n1p2'),
                                           (True, False, '/dev/nvme0n1p2')])
check('W3 ro → remount 成功后写探针通过', ok and any('remount' in l for l in lines), str(why))

_bad = os.path.join(_cam, 'nope', 'deeper')
ok, why, lines = ensure(True, False, _bad)
check('W4 目录不可写 → ok=False（真实写探针判据）', ok is False, 'ok=%s' % ok)
check('W5 失败原因含"不可写"', '不可写' in why, why)

ok, why, lines = ensure(False, False, _cam)
check('W6 未挂载 → ok=False', ok is False, 'ok=%s' % ok)

# 端点层：不可写时必须在**删任何视频之前**中止（409）
_h0b = _hist_len_a = None
code, body = post_execute('edit', plan_count=3, deleted=0,
                          writable=(False, 'cam 分区不可写（仍为只读）'))
check('W7 端点遇不可写 → 409 + success False',
      code == 409 and body.get('success') is False, 'code=%s' % code)
check('W8 端点中止时未删除视频（未调用 execute_cleanup）',
      'execute_cleanup' not in CALLS, '%s' % CALLS)
check('W9 错误信息已回传', '不可写' in (body.get('error') or ''), str(body.get('error')))

_sh.rmtree(_cam, ignore_errors=True)
cr._read_cam_mount = _ORIG_RCM2

# D: 失败/部分失败语义（先记录本组前的历史条数）
def _hist_len():
    return len(json.load(open(cr.CLEANUP_LOG, encoding='utf-8'))) if os.path.exists(cr.CLEANUP_LOG) else 0


_h0 = _hist_len()
code, body = post_execute('edit', plan_count=3, deleted=0,
                          exec_errors=['删除失败 x: [Errno 30] Read-only'],
                          mounts=EDIT_RW)
check('D1 有计划但删 0 → HTTP 500 + success False',
      code == 500 and body.get('success') is False, 'got %s' % code)
code, body = post_execute('edit', plan_count=3, deleted=0, missing=3, mounts=EDIT_RW)
check('D2 计划内文件全 missing → success True（目标已达成）',
      body.get('success') is True, str(body.get('success')))
code, body = post_execute('edit', plan_count=100, deleted=40, mounts=EDIT_RW)
check('D3 只删一部分 → success True 且 partial True',
      body.get('success') is True and body.get('partial') is True,
      'success=%s partial=%s' % (body.get('success'), body.get('partial')))

# G-1 回归：删了 2 个视频 + 2 个孤儿辅助文件（deleted_files=4 > total_count=3）
# 若用 deleted_files 判 partial 会漏报，必须用 breakdown 的视频数
code, body = post_execute('edit', plan_count=3, deleted=2, total_deleted=4, mounts=EDIT_RW)
check('D6 孤儿文件计入 deleted_files 时 partial 仍为 True（G-1 回归）',
      body.get('partial') is True and body.get('success') is True,
      'partial=%s success=%s' % (body.get('partial'), body.get('success')))

# 有计划、删了孤儿但一个视频都没删 → 仍算失败
code, body = post_execute('edit', plan_count=3, deleted=0, total_deleted=5,
                          exec_errors=['删除失败 x: [Errno 30]'], mounts=EDIT_RW)
check('D7 只删了孤儿辅助文件、视频 0 个 → 仍判失败',
      body.get('success') is False and code == 500,
      'code=%s success=%s' % (code, body.get('success')))

hist = json.load(open(cr.CLEANUP_LOG, encoding='utf-8')) if os.path.exists(cr.CLEANUP_LOG) else []
check('D4 D 组 5 次执行只新增 5 条历史（每次 1 条）',
      len(hist) - _h0 == 5, 'delta=%d' % (len(hist) - _h0))
check('D5 历史含 errors/error_count/partial/deleted_files',
      bool(hist) and all(k in hist[0] for k in ('errors', 'error_count', 'partial', 'deleted_files')),
      str(sorted(hist[0].keys())) if hist else 'none')

# E: 历史接口
json.dump([{'timestamp': '2026-10-11T06:12:21', 'deleted_files': 0,
            'errors': ['E%d' % i for i in range(4437)], 'error_count': 4437}],
          open(cr.CLEANUP_LOG, 'w', encoding='utf-8'))
with app.test_client() as c:
    r = c.get('/api/cleanup/history')
    hb = r.get_json()
check('E1 历史接口裁剪 errors 到 5 条',
      len(hb['history'][0]['errors']) == 5, 'len=%d' % len(hb['history'][0]['errors']))
check('E2 保留 error_count=4437', hb['history'][0]['error_count'] == 4437)
check('E3 响应体瘦身 <20KB', len(r.get_data()) < 20000, '%d bytes' % len(r.get_data()))

json.dump([{'timestamp': 'x', 'errors': {'a': 1}}, 'not-a-dict'],
          open(cr.CLEANUP_LOG, 'w', encoding='utf-8'))
with app.test_client() as c:
    r = c.get('/api/cleanup/history')
check('E4 errors 为 dict 的脏数据不 500', r.status_code == 200, 'code=%s' % r.status_code)
check('E5 非 dict 条目不被静默丢弃',
      r.get_json()['history'].__len__() == 2, str(r.get_json()['history']))

os.path.isdir = _real_isdir
print('')
if FAILS:
    print('FAILED: %d -> %s' % (len(FAILS), FAILS))
    sys.exit(1)
print('ALL %d CHECKS PASSED' % N[0])
