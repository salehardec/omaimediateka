#!/usr/bin/env python3
# Тесты Android-источника в bin/oma-mediateka на поддельном gvfs-каталоге
# (`mtp:host=.../Internal shared storage/DCIM/...`). Без телефона и без sudo:
# gvfs, libimobiledevice и pacman подменяются заглушками в PATH, USB — каталогом
# в духе /sys/bus/usb/devices. Запуск: python3 test/test_android.py

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(os.path.dirname(HERE), "bin", "oma-mediateka")

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print("ok   - " + name)
    except Exception as e:  # noqa: BLE001
        failed += 1
        print("FAIL - %s\n     %s" % (name, e))


def assert_eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError("%s: %r != %r" % (what, actual, expected))


def write(path, text="", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


class Env:
    """Песочница: gvfs-каталог, кэш, заглушки команд и фальшивый sysfs USB."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="oma-android-")
        self.run = os.path.join(self.root, "run")
        self.gvfs = os.path.join(self.run, "gvfs")
        self.cache = os.path.join(self.root, "cache")
        self.bin = os.path.join(self.root, "bin")
        self.usb = os.path.join(self.root, "usb")
        self.libs = os.path.join(self.root, "libs")
        for d in (self.gvfs, self.cache, self.bin, self.usb, self.libs):
            os.makedirs(d, exist_ok=True)
        self.gio_list = os.path.join(self.root, "gio-list.txt")
        self.gio_mount_action = os.path.join(self.root, "gio-mount-action.sh")
        self.gio_calls = os.path.join(self.root, "gio.calls")
        self.gio_info = os.path.join(self.root, "gio-info.txt")
        self.gio_bad = os.path.join(self.root, "gio.bad")
        self.udids = os.path.join(self.root, "udids.txt")
        write(self.gio_list, "")
        write(self.udids, "")
        # gio: `mount -l` печатает заготовку, `mount <uri>` выполняет действие.
        self.touch = os.path.join(self.root, "touch.log")
        self.lock = os.path.join(self.root, "mtp.lock")
        # «Обращения к телефону» (touch.log): монтирование, gio info, find по mtp-тому.
        write(os.path.join(self.bin, "gio"),
              '#!/bin/sh\n'
              'if [ "$1" = mount ] && [ "$2" = -li ]; then cat "%(list)s"; exit 0; fi\n'
              'if [ "$1" = info ]; then echo info >> "%(touch)s"; [ -s "%(info)s" ] && cat "%(info)s"; exit 0; fi\n'
              'if [ "$1" = mount ]; then echo "$2" >> "%(calls)s"; echo mount >> "%(touch)s"; '
              '[ -f "%(action)s" ] && . "%(action)s"; [ -f "%(action)s" ]; exit $?; fi\n'
              'echo "unexpected gio $*" >> "%(bad)s"; exit 1\n'
              % dict(list=self.gio_list, touch=self.touch, info=self.gio_info,
                     calls=self.gio_calls, action=self.gio_mount_action, bad=self.gio_bad),
              0o755)
        write(os.path.join(self.bin, "find"),
              '#!/bin/sh\ncase "$*" in *mtp:host=*) echo find >> "%s";; esac\n'
              'exec /usr/bin/find "$@"\n' % self.touch, 0o755)
        write(os.path.join(self.bin, "idevice_id"),
              '#!/bin/sh\ncat "%s"\n' % self.udids, 0o755)
        self.product_type = os.path.join(self.root, "product-type.txt")
        write(self.product_type, "")
        write(os.path.join(self.bin, "ideviceinfo"),
              '#!/bin/sh\n[ -s "%s" ] && cat "%s"\n' % (self.product_type, self.product_type), 0o755)
        write(os.path.join(self.bin, "idevicepair"), "#!/bin/sh\nexit 0\n", 0o755)
        write(os.path.join(self.bin, "pacman"), "#!/bin/sh\nexit 1\n", 0o755)
        self.env = dict(
            os.environ,
            XDG_RUNTIME_DIR=self.run,
            XDG_CACHE_HOME=self.cache,
            PATH=self.bin + ":/usr/bin:/bin",
            OMA_USB_SYSFS=self.usb,
            OMA_GVFS_LIBDIRS=self.libs,
            OMA_MTP_LOCK=self.lock,
            OMA_NOW="1000",
        )

    def at(self, t):
        self.env["OMA_NOW"] = str(t)

    def touches(self):
        if not os.path.exists(self.touch):
            return 0
        return len(open(self.touch).read().split())

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def install_mtp_backend(self):
        write(os.path.join(self.libs, "gvfsd-mtp"), "#!/bin/sh\n", 0o755)

    def usb_device(self, name, vendor, classes=(), product=None):
        d = os.path.join(self.usb, name)
        write(os.path.join(d, "idVendor"), vendor + "\n")
        if product:
            write(os.path.join(d, "product"), product + "\n")
        for i, cls in enumerate(classes):
            write(os.path.join(d, "%s:1.%d" % (name, i), "bInterfaceClass"), cls + "\n")

    def phone(self, host="Google_Pixel_7_ABC123", storages=("Internal shared storage",),
              files=None):
        """Создаёт mtp-том с медиа. Возвращает путь тома."""
        mount = os.path.join(self.gvfs, "mtp:host=" + host)
        os.makedirs(mount, exist_ok=True)
        for st in storages:
            os.makedirs(os.path.join(mount, st), exist_ok=True)
        for rel in files or []:
            write(os.path.join(mount, rel), "data")
        return mount

    def iphone(self, udid="00008130-TESTUDID", files=("DCIM/100APPLE/IMG_0001.JPG",)):
        write(self.udids, udid + "\n")
        mount = os.path.join(self.gvfs, "afc:host=" + udid)
        os.makedirs(mount, exist_ok=True)
        for rel in files:
            write(os.path.join(mount, rel), "data")
        return mount

    def helper(self, *args):
        p = subprocess.run(["bash", HELPER] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=60)
        return p

    def status(self, *args):
        p = self.helper("status", *args)
        assert_eq(p.returncode, 0, "status rc: " + p.stderr)
        return json.loads(p.stdout)


def with_env(fn):
    def wrapper():
        e = Env()
        try:
            fn(e)
        finally:
            e.close()
    return wrapper


MEDIA = [
    "Internal shared storage/DCIM/Camera/PXL_20261013_093000123.jpg",
    "Internal shared storage/DCIM/Camera/VID_20261012_180000.mp4",
    "Internal shared storage/DCIM/Screenshots/Screenshot_1.png",
    "Internal shared storage/Pictures/Telegram/photo 1.jpg",
]


# --- status -----------------------------------------------------------------

def t_no_device(e):
    e.install_mtp_backend()
    s = e.status()
    assert_eq(s["state"], "no-device", "state")
    assert_eq(s["type"], "none", "type")
    assert_eq(s["devices"], [], "devices")
    assert_eq(s["roots"], [], "roots")


def t_android_ready(e):
    e.install_mtp_backend()
    mount = e.phone(files=MEDIA)
    write(e.gio_list, "Mount(0): Pixel 7 -> mtp://Google_Pixel_7_ABC123/\n  Type: GDaemonMount\n")
    s = e.status()
    assert_eq(s["state"], "ready", "state")
    assert_eq(s["type"], "android", "type")
    assert_eq(s["name"], "Pixel 7", "name")
    assert_eq(s["id"], "Google_Pixel_7_ABC123", "id")
    assert_eq(s["udid"], "", "udid только для iPhone")
    assert_eq(s["mount"], mount, "mount")
    st = os.path.join(mount, "Internal shared storage")
    assert_eq(s["roots"], [st + "/DCIM", st + "/Pictures"], "roots")
    assert_eq(s["dcim"], st + "/DCIM", "dcim (первый корень)")
    assert_eq(s["devices"], [{"type": "android", "name": "Pixel 7",
                              "id": "Google_Pixel_7_ABC123", "state": "ready"}], "devices")


def t_android_sd_card(e):
    e.install_mtp_backend()
    mount = e.phone(storages=("Internal storage", "SD card"), files=[
        "Internal storage/DCIM/Camera/a.jpg",
        "SD card/DCIM/Camera/b.jpg",
        "SD card/Pictures/c.jpg",
    ])
    s = e.status()
    assert_eq(s["state"], "ready", "state")
    assert_eq(s["roots"], [
        mount + "/Internal storage/DCIM",
        mount + "/SD card/DCIM",
        mount + "/SD card/Pictures",
    ], "roots")
    # без строки Mount(...) имя берётся из host
    assert_eq((s["name"], s["model"]), ("Android", ""), "без имени от телефона — общее «Android»")


def t_android_locked(e):
    e.install_mtp_backend()
    e.phone(storages=())  # том смонтирован, хранилищ нет — телефон заблокирован
    s = e.status()
    assert_eq(s["state"], "locked", "state")
    assert_eq(s["type"], "android", "type")
    assert "Передача файлов" in s["message"], s["message"]
    assert_eq(s["roots"], [], "roots")


def t_android_no_media_dirs(e):
    e.install_mtp_backend()
    e.phone(files=["Internal shared storage/Download/doc.pdf"])
    s = e.status()
    assert_eq(s["state"], "no-dcim", "state")
    assert "DCIM" in s["message"], s["message"]


def t_android_charge_only(e):
    e.install_mtp_backend()
    e.usb_device("1-4", "18d1")  # Google, интерфейсов нет — только зарядка
    s = e.status()
    assert_eq(s["state"], "charge-only", "state")
    assert_eq(s["type"], "android", "type")
    assert "Передача файлов" in s["message"], s["message"]
    assert "зарядк" in s["message"], s["message"]
    assert_eq(s["devices"][0]["state"], "charge-only", "devices")


def t_android_charge_only_with_adb_interface(e):
    e.install_mtp_backend()
    e.usb_device("1-4", "04e8", classes=("ff",))  # Samsung, только adb
    assert_eq(e.status()["state"], "charge-only", "state")


def t_non_phone_usb_not_android(e):
    e.install_mtp_backend()
    e.usb_device("1-5", "04e8", classes=("08",))  # Samsung SSD: mass storage
    e.usb_device("1-6", "046d")  # посторонний производитель
    assert_eq(e.status()["state"], "no-device", "state")


def t_android_no_backend(e):
    # gvfs-mtp не установлен (pacman-заглушка отвечает 1, демона нет)
    e.usb_device("1-4", "2717")
    s = e.status()
    assert_eq(s["state"], "no-backend", "state")
    assert_eq(s["type"], "android", "type")
    assert "gvfs-mtp" in s["message"], s["message"]


def t_android_mounts_volume(e):
    e.install_mtp_backend()
    host = "Google_Pixel_7_ABC123"
    write(e.gio_list, "Volume(0): Pixel 7\n    activation_root=mtp://%s/\n" % host)
    mount = os.path.join(e.gvfs, "mtp:host=" + host)
    write(e.gio_mount_action,
          'mkdir -p "%s/Internal shared storage/DCIM/Camera"\n' % mount)
    s = e.status()
    assert_eq(s["state"], "ready", "state")
    calls = open(e.gio_calls).read().split()
    assert_eq(calls, ["mtp://%s/" % host], "gio mount вызван с URI тома")
    assert not os.path.exists(e.gio_bad), "gio вызван не только для mount"


def t_android_volume_wont_mount_is_locked(e):
    e.install_mtp_backend()
    write(e.gio_list, "Volume(0): Pixel 7\n    activation_root=mtp://Pixel_X/\n")
    # gio mount завершается ошибкой (нет файла действия) — телефон заблокирован
    s = e.status()
    assert_eq(s["state"], "locked", "state")
    assert_eq(s["type"], "android", "type")


def t_iphone_ready_unchanged(e):
    e.install_mtp_backend()
    mount = e.iphone()
    s = e.status()
    assert_eq(s["state"], "ready", "state")
    assert_eq(s["type"], "iphone", "type")
    assert_eq(s["udid"], "00008130-TESTUDID", "udid")
    assert_eq(s["id"], "00008130-TESTUDID", "id")
    assert_eq(s["dcim"], mount + "/DCIM", "dcim")
    assert_eq(s["roots"], [mount + "/DCIM"], "roots")
    assert_eq(s["name"], "iPhone", "name")


def t_both_connected(e):
    e.install_mtp_backend()
    iph = e.iphone()
    and_ = e.phone(files=MEDIA)
    s = e.status()
    # без предпочтения — iPhone раньше Android
    assert_eq(s["type"], "iphone", "type по умолчанию")
    assert_eq(s["mount"], iph, "mount")
    assert_eq([d["type"] for d in s["devices"]], ["iphone", "android"], "devices")
    a = e.status("android")
    assert_eq(a["type"], "android", "type android")
    assert_eq(a["mount"], and_, "mount android")
    assert_eq(len(a["devices"]), 2, "devices у обоих")
    i = e.status("iphone")
    assert_eq(i["type"], "iphone", "type iphone")


def t_both_prefers_ready_one(e):
    e.install_mtp_backend()
    write(e.udids, "00008130-TESTUDID\n")  # iPhone есть, но том не смонтирован
    e.phone(files=MEDIA)
    s = e.status()
    assert_eq(s["type"], "android", "готовое устройство выбирается первым")
    assert_eq(s["state"], "ready", "state")
    # явный выбор пользователя побеждает
    assert_eq(e.status("iphone")["type"], "iphone", "iphone по просьбе")


def t_prefer_missing_device_falls_back(e):
    e.install_mtp_backend()
    e.phone(files=MEDIA)
    assert_eq(e.status("iphone")["type"], "android", "iPhone нет — показываем Android")


# --- list / thumb / meta / copy ----------------------------------------------

def tree_snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size, os.path.isdir(p))
    return out


def t_list_android_roots(e):
    e.install_mtp_backend()
    mount = e.phone(files=MEDIA + [
        "Internal shared storage/DCIM/.thumbnails/1234.jpg",
        "Internal shared storage/DCIM/Camera/.trashed-1700000000-IMG_1.jpg",
        "Internal shared storage/DCIM/Camera/notes.txt",
        "Internal shared storage/DCIM/Camera/clip.3gp",
        "Internal shared storage/DCIM/Camera/clip2.WEBM",
        "Internal shared storage/Download/other.jpg",  # вне DCIM/Pictures
    ])
    st = mount + "/Internal shared storage"
    p = e.helper("list", st + "/DCIM", st + "/Pictures")
    assert_eq(p.returncode, 0, "rc")
    rows = [l.split("\t") for l in p.stdout.splitlines()]
    paths = sorted(r[2] for r in rows)
    assert_eq(paths, sorted([
        st + "/DCIM/Camera/PXL_20261013_093000123.jpg",
        st + "/DCIM/Camera/VID_20261012_180000.mp4",
        st + "/DCIM/Camera/clip.3gp",
        st + "/DCIM/Camera/clip2.WEBM",
        st + "/DCIM/Screenshots/Screenshot_1.png",
        st + "/Pictures/Telegram/photo 1.jpg",
    ]), "paths")
    for r in rows:
        assert_eq(len(r), 3, "mtime<TAB>size<TAB>path")
        float(r[0])
        int(r[1])


def t_list_missing_dir_is_empty(e):
    p = e.helper("list", "/nonexistent/dir")
    assert_eq((p.returncode, p.stdout), (0, ""), "пустой, без ошибки")
    assert_eq(e.helper("list").returncode, 2, "без аргументов — usage")


def t_list_cache(e):
    e.install_mtp_backend()
    mount = e.phone(files=MEDIA)
    st = mount + "/Internal shared storage"
    assert_eq(e.helper("list-cached", "android-Pixel").returncode, 1, "кэша ещё нет")
    p = e.helper("list", "--cache", "android-Pixel_7/x", st + "/DCIM", st + "/Pictures")
    assert_eq(p.returncode, 0, "rc")
    cached = os.path.join(e.cache, "omaimediateka", "lists", "android-Pixel_7_x.tsv")
    assert os.path.isfile(cached), "кэш записан под очищенным ключом"
    c = e.helper("list-cached", "android-Pixel_7/x")
    assert_eq(c.returncode, 0, "list-cached rc")
    assert_eq(c.stdout, p.stdout, "кэш совпадает с листингом")
    # без --cache ничего не пишется
    e2 = e.helper("list", st + "/DCIM")
    assert_eq(e2.returncode, 0, "rc2")
    assert_eq(sorted(os.listdir(os.path.dirname(cached))), ["android-Pixel_7_x.tsv"], "один файл")


def t_list_iphone_unchanged(e):
    mount = e.iphone(files=[
        "DCIM/100APPLE/IMG_0001.JPG",
        "DCIM/100APPLE/IMG_0001.AAE",
        "DCIM/100APPLE/IMG_0002.HEIC",
        "DCIM/101APPLE/IMG_0100.MOV",
        "DCIM/100APPLE/.hidden.jpg",
        "DCIM/100APPLE/__MACOSX/x.jpg",
        "DCIM/100APPLE/song.mp3",
    ])
    p = e.helper("list", mount + "/DCIM")
    paths = sorted(l.split("\t")[2] for l in p.stdout.splitlines())
    assert_eq(paths, [
        mount + "/DCIM/100APPLE/IMG_0001.JPG",
        mount + "/DCIM/100APPLE/IMG_0002.HEIC",
        mount + "/DCIM/101APPLE/IMG_0100.MOV",
    ], "paths")


def make_media(path_jpg, path_mp4):
    os.makedirs(os.path.dirname(path_jpg), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=640x480:rate=1:duration=1", "-frames:v", "1", path_jpg],
                   check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=10:duration=2", path_mp4], check=True)


def t_thumb_meta_copy_on_mock_volume(e):
    if not shutil.which("ffmpeg"):
        raise AssertionError("ffmpeg нужен для теста превью")
    e.install_mtp_backend()
    mount = e.phone()
    cam = mount + "/Internal shared storage/DCIM/Camera"
    jpg, mp4 = cam + "/PXL one.jpg", cam + "/VID one.mp4"
    make_media(jpg, mp4)
    before = tree_snapshot(mount)
    thumbs = os.path.join(e.root, "out")
    # фото
    d1 = thumbs + "/t1.jpg"
    assert_eq(e.helper("thumb", jpg, d1, "photo", "thumb").returncode, 0, "thumb photo")
    assert os.path.getsize(d1) > 0
    # видео
    d2 = thumbs + "/t2.jpg"
    assert_eq(e.helper("thumb", mp4, d2, "video", "preview").returncode, 0, "thumb video")
    assert os.path.getsize(d2) > 0
    # meta
    m = e.helper("meta", mp4)
    dur, w, h = m.stdout.strip().split("\t")
    assert_eq((round(float(dur)), w, h), (2, "320", "240"), "meta")
    # copy: оригинал не тронут, приёмник — копия
    dst = os.path.join(e.root, "export", "PXL one.jpg")
    c = e.helper("copy", jpg, dst)
    assert_eq(c.returncode, 0, "copy rc")
    assert_eq(open(dst, "rb").read(), open(jpg, "rb").read(), "copy content")
    assert_eq(e.helper("copy", jpg, dst).returncode, 3, "не перезаписывает")
    # телефон не изменился
    assert_eq(tree_snapshot(mount), before, "mock-том не изменён")


def t_helper_never_writes_to_device(e):
    # Статически: в хелпере нет операций, меняющих содержимое телефона.
    text = open(HELPER).read()
    for bad in ("gio remove", "gio trash", "gio copy", "gio move", "gio rename",
                "gio save", "adb "):
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert bad not in code, "запрещено в хелпере: %r в %r" % (bad, line)
    assert "rm -rf \"$td\"" in text  # rm только во временных каталогах


# --- поиск по содержимому недоступен для Android -----------------------------

def t_index_and_search_refuse_android(e):
    e.install_mtp_backend()
    mount = e.phone(files=MEDIA)
    p = e.helper("index", mount)
    assert_eq(p.returncode, 5, "index rc")
    assert "iPhone" in p.stderr, p.stderr
    # без iPhone и с Android search тоже отказывает, а не молча возвращает пусто
    s = e.helper("search", "море")
    assert_eq(s.returncode, 5, "search rc")
    assert_eq(s.stdout, "", "search stdout")
    # индекс не создан
    assert not os.path.exists(os.path.join(e.cache, "omaimediateka", "search.sqlite"))


def t_search_still_available_with_iphone(e):
    e.install_mtp_backend()
    e.iphone()
    e.phone(files=MEDIA)
    # с iPhone гард не срабатывает: без базы код прежний (1), не 5
    assert_eq(e.helper("search", "море").returncode, 1, "search rc без индекса")


# --- модель устройства для плашки ---------------------------------------------

def t_model_android_from_mtp_name(e):
    e.install_mtp_backend()
    e.phone(host="Google_Pixel_8_XYZ")
    write(e.gio_list, "Mount(0): Pixel 8 -> mtp://Google_Pixel_8_XYZ/\n")
    assert_eq(e.status()["model"], "Pixel 8", "model")


def t_model_android_from_gio_info_display_name(e):
    # Реальный случай: два Mount в gio, один общий «mtp»; имя — из `gio info`,
    # без хвоста с именем владельца.
    e.install_mtp_backend()
    host = "SAMSUNG_SAMSUNG_Android_2a3c"
    e.phone(host=host)
    write(e.gio_list, "Mount(0): SAMSUNG Android -> mtp://%s/\nMount(1): mtp -> mtp://%s/\n" % (host, host))
    write(e.gio_info, "display name: Galaxy S9+ пользователя Алексей\nname: %s\n" % host)
    s = e.status()
    assert_eq((s["model"], s["name"]), ("Galaxy S9+", "Galaxy S9+"), "model/name")


def t_model_android_generic_name_uses_usb_product(e):
    e.install_mtp_backend()
    e.phone(host="SAMSUNG_SAMSUNG_Android_R58")
    write(e.gio_list, "Mount(0): SAMSUNG_Android -> mtp://SAMSUNG_SAMSUNG_Android_R58/\n")
    e.usb_device("1-4", "04e8", product="SM-S911B")
    assert_eq(e.status()["model"], "SM-S911B", "model из USB product")


def t_model_android_charge_only_from_usb(e):
    e.install_mtp_backend()
    e.usb_device("1-4", "18d1", product="Pixel 8")
    s = e.status()
    assert_eq((s["state"], s["model"]), ("charge-only", "Pixel 8"), "state/model")


def t_model_android_unknown_is_empty(e):
    e.install_mtp_backend()
    e.phone(host="Some_Phone")
    s = e.status()
    assert_eq(s["model"], "", "модели нет — пусто, имя остаётся запасным")
    assert_eq(s["name"], "Android", "name")


def t_model_iphone_marketing_name(e):
    e.install_mtp_backend()
    e.iphone()
    write(e.product_type, "iPhone16,1\n")
    assert_eq(e.status()["model"], "iPhone 15 Pro", "model")


def t_model_iphone_unknown_product_type_kept(e):
    e.install_mtp_backend()
    e.iphone()
    write(e.product_type, "iPhone99,9\n")
    assert_eq(e.status()["model"], "iPhone99,9", "ProductType как есть")


def t_model_iphone_unavailable_is_empty(e):
    e.install_mtp_backend()
    e.iphone()
    assert_eq(e.status()["model"], "", "ideviceinfo молчит — модели нет")


def t_model_empty_without_device(e):
    e.install_mtp_backend()
    assert_eq(e.status()["model"], "", "model")


# --- бережное обращение с MTP: бэкофф, одна попытка, лок --------------------

def t_backoff_after_locked_no_device_access(e):
    e.install_mtp_backend()
    e.phone(storages=())  # том пуст: «Get Storage information failed»
    e.at(1000)
    s = e.status()
    assert_eq(s["state"], "locked", "state")
    first = e.touches()
    assert first > 0, "первая проверка обращается к тому"
    for t in (1005, 1015, 1029):  # внутри бэкоффа 30 с
        e.at(t)
        s = e.status()
        assert_eq(s["state"], "locked", "state при t=%d" % t)
        assert_eq(e.touches(), first, "к телефону нельзя обращаться при t=%d" % t)
    # бэкофф не короче 30 с
    state = open(os.path.join(e.cache, "omaimediateka", "android-state", "android.state")).read()
    fields = dict(l.split("=", 1) for l in state.splitlines() if l.startswith(("S_TS=", "S_UNTIL=")))
    assert int(fields["S_UNTIL"]) - int(fields["S_TS"]) >= 30, fields
    # после бэкоффа — одна новая проверка
    e.at(1031)
    e.status()
    second = e.touches()
    assert second > first, "после бэкоффа проверка повторяется"
    e.at(1032)
    e.status()
    assert_eq(e.touches(), second, "и снова пауза")


def t_backoff_grows_on_repeated_locked(e):
    e.install_mtp_backend()
    e.phone(storages=())
    seen = []
    t = 1000
    for _ in range(6):
        e.at(t)
        e.status()
        state = open(os.path.join(e.cache, "omaimediateka", "android-state", "android.state")).read()
        f = dict(l.split("=", 1) for l in state.splitlines() if l.startswith(("S_TS=", "S_UNTIL=")))
        gap = int(f["S_UNTIL"]) - int(f["S_TS"])
        seen.append(gap)
        t = int(f["S_UNTIL"]) + 1
    assert_eq(seen, [30, 60, 120, 240, 300, 300], "паузы растут до 300 с")


def t_connection_key_uses_the_mtp_volume_device(e):
    e.install_mtp_backend()
    drives = ("Drive(0): SSD\n  Type: GProxyDrive\nVolume(0): Disk\n  ids:\n"
              "   unix-device: '/dev/nvme0n1'\n  activation_root=file:///\n")
    vol = ("Volume(1): Pixel 7\n  ids:\n   unix-device: '/dev/bus/usb/005/%s'\n"
           "  activation_root=mtp://Pixel_X/\n")
    write(e.gio_list, drives + vol % "033")
    e.at(1000)
    e.status()
    state = open(os.path.join(e.cache, "omaimediateka", "android-state", "android.state")).read()
    assert "/dev/bus/usb/005/033" in state and "nvme0n1" not in state, state
    # переподключение (новый номер устройства) — новая единственная попытка
    write(e.gio_list, drives + vol % "034")
    e.at(1100)
    e.status()
    assert_eq(len(open(e.gio_calls).read().split()), 2, "повторное подключение распознано")


def t_force_overrides_backoff_but_is_throttled(e):
    e.install_mtp_backend()
    e.phone(storages=())
    e.at(1000)
    e.status()
    base = e.touches()
    e.at(1004)  # слишком скоро даже для явного запроса (минимум 10 с)
    e.status("--force")
    assert_eq(e.touches(), base, "force чаще 10 с игнорируется")
    e.at(1012)
    e.status("--force")
    assert e.touches() > base, "явный запрос проходит мимо бэкоффа"


def t_ready_state_reused_without_touching_phone(e):
    e.install_mtp_backend()
    e.phone(files=MEDIA)
    e.at(1000)
    s = e.status()
    assert_eq(s["state"], "ready", "state")
    base = e.touches()
    e.at(1030)
    s2 = e.status()
    assert_eq(s2["state"], "ready", "state из кэша")
    assert_eq(s2["roots"], s["roots"], "roots из кэша")
    assert_eq(e.touches(), base, "в пределах TTL телефон не трогаем")
    e.at(1061)
    e.status()
    assert e.touches() > base, "после TTL — одна проверка"


def t_mount_attempted_once_per_connection(e):
    e.install_mtp_backend()
    vol = ("Volume(0): Pixel 7\n  ids:\n   unix-device: '/dev/bus/usb/005/%s'\n"
           "  activation_root=mtp://Pixel_X/\n")
    write(e.gio_list, vol % "033")

    def mounts():
        return open(e.gio_calls).read().split() if os.path.exists(e.gio_calls) else []

    e.at(1000)
    assert_eq(e.status()["state"], "locked", "state")
    assert_eq(len(mounts()), 1, "первая попытка монтирования")
    for t in (1100, 1300, 5000):  # бэкофф давно прошёл, но подключение то же
        e.at(t)
        s = e.status()
        assert_eq(s["state"], "locked", "state")
        assert_eq(len(mounts()), 1, "повторно не монтируем (t=%d)" % t)
    assert "Проверить снова" in s["message"], s["message"]
    e.at(6000)
    e.status("--force")
    assert_eq(len(mounts()), 2, "force монтирует явно")
    write(e.gio_list, vol % "034")  # переподключение: другой номер USB-устройства
    e.at(7000)
    e.status()
    assert_eq(len(mounts()), 3, "новое подключение — одна попытка")
    e.at(8000)
    e.status()
    assert_eq(len(mounts()), 3, "и больше не повторяет")


def t_status_does_not_wait_for_busy_lock(e):
    e.install_mtp_backend()
    e.phone(files=MEDIA)
    e.at(1000)
    ready = e.status()
    assert_eq(ready["state"], "ready", "state")
    base = e.touches()
    fd = os.open(e.lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # другой процесс держит телефон
    try:
        e.at(5000)  # TTL давно вышел — без лока была бы новая проверка
        t0 = time.time()
        busy = e.status()
        assert time.time() - t0 < 5, "status не ждёт лок"
        assert_eq(busy["state"], "ready", "последнее известное состояние")
        assert_eq(busy["roots"], ready["roots"], "roots")
        assert_eq(e.touches(), base, "к занятому телефону не обращаемся")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    e.at(5001)
    e.status()
    assert e.touches() > base, "после освобождения лока проверка проходит"


def t_status_busy_lock_without_cache_says_checking(e):
    e.install_mtp_backend()
    e.phone(files=MEDIA)
    fd = os.open(e.lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        s = e.status()
        assert_eq(s["state"], "mounting", "state")
        assert_eq(s["type"], "android", "type")
        assert_eq(e.touches(), 0, "телефон не тронут")
    finally:
        os.close(fd)


def t_data_commands_queue_on_device_lock(e):
    e.install_mtp_backend()
    mount = e.phone(files=MEDIA)
    st = mount + "/Internal shared storage"
    local = os.path.join(e.root, "local")
    write(os.path.join(local, "DCIM", "a.jpg"), "x")
    fd = os.open(e.lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    e.env["OMA_MTP_LOCK_WAIT"] = "1"
    try:
        p = e.helper("list", st + "/DCIM")
        assert_eq(p.returncode, 98, "list по MTP ждёт очередь и сдаётся")
        p = e.helper("meta", st + "/DCIM/Camera/PXL_20261013_093000123.jpg")
        assert_eq(p.returncode, 98, "meta по MTP тоже в очереди")
        p = e.helper("list", os.path.join(local, "DCIM"))  # не-MTP лок не касается
        assert_eq(p.returncode, 0, "локальный list")
        assert "a.jpg" in p.stdout, p.stdout
        dst = os.path.join(e.root, "cached.jpg")  # превью в кэше — без телефона и лока
        write(dst, "jpeg")
        p = e.helper("thumb", st + "/DCIM/Camera/PXL_20261013_093000123.jpg", dst, "photo")
        assert_eq(p.returncode, 0, "кэш-хит thumb")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    p = e.helper("list", st + "/DCIM")
    assert_eq(p.returncode, 0, "после освобождения лока list проходит")
    assert "PXL_20261013_093000123.jpg" in p.stdout, p.stdout


def main():
    cases = [
        ("status: нет устройства", t_no_device),
        ("status: Android ready — тип, имя, id, корни", t_android_ready),
        ("status: Android с SD-картой — второе хранилище", t_android_sd_card),
        ("status: Android заблокирован → locked с подсказкой", t_android_locked),
        ("status: Android без DCIM/Pictures → no-dcim", t_android_no_media_dirs),
        ("status: Android в режиме зарядки → charge-only", t_android_charge_only),
        ("status: Android с одним adb-интерфейсом → charge-only", t_android_charge_only_with_adb_interface),
        ("status: SSD и чужие USB-устройства — не Android", t_non_phone_usb_not_android),
        ("status: Android без gvfs-mtp → no-backend", t_android_no_backend),
        ("status: том виден, но не смонтирован — gio mount по URI", t_android_mounts_volume),
        ("status: том не монтируется → locked", t_android_volume_wont_mount_is_locked),
        ("status: iPhone — прежние поля и тип", t_iphone_ready_unchanged),
        ("status: оба подключены — iPhone по умолчанию, выбор по аргументу", t_both_connected),
        ("status: оба подключены — готовое устройство первым", t_both_prefers_ready_one),
        ("status: запрошенного устройства нет — запасной показ", t_prefer_missing_device_falls_back),
        ("model: Android из MTP-имени", t_model_android_from_mtp_name),
        ("model: Android из gio info без имени владельца", t_model_android_from_gio_info_display_name),
        ("model: общее MTP-имя → USB product", t_model_android_generic_name_uses_usb_product),
        ("model: Android в режиме зарядки — из USB", t_model_android_charge_only_from_usb),
        ("model: Android без модели — пусто", t_model_android_unknown_is_empty),
        ("model: iPhone ProductType → «iPhone 15 Pro»", t_model_iphone_marketing_name),
        ("model: неизвестный ProductType остаётся как есть", t_model_iphone_unknown_product_type_kept),
        ("model: iPhone без ideviceinfo-ответа — пусто", t_model_iphone_unavailable_is_empty),
        ("model: нет устройства — пусто", t_model_empty_without_device),
        ("бэкофф ≥30 с: после locked к телефону не обращаемся", t_backoff_after_locked_no_device_access),
        ("бэкофф растёт при повторном locked до 300 с", t_backoff_grows_on_repeated_locked),
        ("ключ подключения берётся от тома MTP, а не от дисков", t_connection_key_uses_the_mtp_volume_device),
        ("force обходит бэкофф, но не чаще 10 с", t_force_overrides_backoff_but_is_throttled),
        ("ready переиспользуется в пределах TTL без обращений", t_ready_state_reused_without_touching_phone),
        ("монтирование: одна попытка на подключение", t_mount_attempted_once_per_connection),
        ("лок: status не ждёт занятый телефон, отдаёт кэш", t_status_does_not_wait_for_busy_lock),
        ("лок: нет кэша и занято → «проверяю», телефон не тронут", t_status_busy_lock_without_cache_says_checking),
        ("лок: list/meta по MTP стоят в очереди, кэш thumb — нет", t_data_commands_queue_on_device_lock),
        ("list: несколько корней, скрытое и не-медиа отброшено", t_list_android_roots),
        ("list: пустой каталог и usage", t_list_missing_dir_is_empty),
        ("list --cache / list-cached: кэш листинга", t_list_cache),
        ("list: iPhone — тот же набор файлов", t_list_iphone_unchanged),
        ("thumb/meta/copy на mock-томе, телефон не изменён", t_thumb_meta_copy_on_mock_volume),
        ("хелпер не пишет и не удаляет на телефоне", t_helper_never_writes_to_device),
        ("index/search для Android → код 5", t_index_and_search_refuse_android),
        ("search с iPhone — без гарда", t_search_still_available_with_iphone),
    ]
    for name, fn in cases:
        test(name, with_env(fn))
    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
