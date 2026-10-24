# PlatformIO extra script: "pio run -e ota -t upload" sends the firmware to the board over Wi-Fi,
# through the board's own page /update (HTTP POST, login admin + the admin password).
# The password lives in secrets.ini (not in git):
#   [ota]
#   host = svitlo-local.local      ; or the board's IP
#   password = ...
# A firmware built with public.key accepts only signed images, so then firmware.bin.signed is sent
# (signing.py makes it after every build when private.key is on this PC).
import base64
import os
import uuid
import urllib.request

Import("env")  # noqa: F821  (provided by PlatformIO)


def ota_upload(source, target, env):
    cfg = env.GetProjectConfig()
    if not cfg.has_section("ota") or not cfg.has_option("ota", "password"):
        print("secrets.ini with [ota] password = ... is missing")
        return 1
    host = cfg.get("ota", "host") if cfg.has_option("ota", "host") else "svitlo-local.local"
    password = cfg.get("ota", "password")
    path = str(source[0])
    if os.path.exists(os.path.join(env.subst("$PROJECT_DIR"), "public.key")):
        path += ".signed"
        if not os.path.exists(path):
            print("firmware.bin.signed is missing (no private.key on this PC?): a board with signing "
                  "turned on would reject an unsigned image. Upload over USB instead.")
            return 1
    with open(path, "rb") as f:
        image = f.read()

    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"firmware.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + image + f"\r\n--{boundary}--\r\n".encode()
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    req = urllib.request.Request(f"http://{host}/update", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Basic {auth}",
    })
    print(f"Uploading {len(image)} bytes to http://{host}/update ...")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode(errors="replace")
    except Exception as exc:  # 401 = wrong password, timeout = board not reachable
        print(f"OTA upload failed: {exc}")
        return 1
    print(text.strip()[:200])
    return 0 if "Success" in text else 1


env.Replace(UPLOADCMD=ota_upload)  # noqa: F821
