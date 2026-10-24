#include "golden.h"

#include <LittleFS.h>
#include <MD5Builder.h>
#include <Updater.h>

#include "ota.h"
#include "remote.h"
#include "state.h"

static const uint32_t SECTOR = 4096;
static const uint32_t FS_SPARE = 64 * 1024;  // left free in LittleFS for the settings and logs
static const uint32_t TRIAL_MS = 15UL * 60 * 1000;
static const uint32_t TRIAL_BOOTS = 4;  // this start included: 3 starts that did not get to a confirmation

static const char *IMAGE_FILE = "/golden.bin";     // the golden image itself
static const char *META_FILE = "/golden";          // "<version> <size> <md5>" of the golden copy
static const char *CONFIRMED_FILE = "/confirmed";  // the last version that proved itself
static const char *TRIAL_FILE = "/trial";          // "<version> <starts>" of the version on trial

static String gVer, gMd5;
static uint32_t gSize = 0;
static bool trial = false;
static uint32_t trialStarts = 0;
static bool autoSaveTried = false;
static String lastSave;

static String readFile(const char *path) {
  File f = LittleFS.open(path, "r");
  if (!f) return "";
  String s = f.readString();
  f.close();
  s.trim();
  return s;
}

static void writeFile(const char *path, const String &s) {
  File f = LittleFS.open(path, "w");
  if (!f) return;
  f.print(s);
  f.close();
}

static void loadMeta() {
  String m = readFile(META_FILE);
  int a = m.indexOf(' '), b = m.indexOf(' ', a + 1);
  if (a <= 0 || b < 0) {
    gVer = gMd5 = "";
    gSize = 0;
    return;
  }
  gVer = m.substring(0, a);
  gSize = strtoul(m.substring(a + 1, b).c_str(), nullptr, 10);
  gMd5 = m.substring(b + 1);
  if (!gSize || gMd5.length() != 32) gVer = "";
  File f = LittleFS.open(IMAGE_FILE, "r");
  if (!f || f.size() != gSize) gVer = "";  // a description without its image (or a cut one) is no copy
  if (f) f.close();
}

static String fileMd5(uint32_t size) {
  File f = LittleFS.open(IMAGE_FILE, "r");
  if (!f || f.size() != size) return "";
  MD5Builder md5;
  md5.begin();
  md5.addStream(f, size);
  f.close();
  md5.calculate();
  return md5.toString();
}

static void forget() {  // the copy is unusable or about to be replaced: never trust it again
  LittleFS.remove(META_FILE);
  LittleFS.remove(IMAGE_FILE);
  gVer = gMd5 = "";
  gSize = 0;
}

static void endTrial() {
  trial = false;
  LittleFS.remove(TRIAL_FILE);
}

// Stage the golden image with Update (the boot loader copies it over the sketch) and restart.
static void rollback(const String &why) {
  String fail;
  if (fileMd5(gSize) != gMd5) fail = F("запасна копія пошкоджена");
  File f;
  if (!fail.length()) f = LittleFS.open(IMAGE_FILE, "r");
  if (!fail.length() && !f) fail = F("немає файлу запасної копії");
  if (!fail.length()) {
    // Our own copy, checked by MD5 above; it has no RSA signature (it was copied from flash).
    Update.installSignature(nullptr, nullptr);
    if (!Update.begin(gSize, U_FLASH) || Update.writeStream(f) != gSize || !Update.end()) {
      fail = String(F("помилка відновлення: ")) + Update.getErrorString();
      Update.end(true);
    }
  }
  if (f) f.close();
  if (fail.length()) {
    Serial.printf("golden: cannot return (%s), staying on this version\n", fail.c_str());
    otaNote(String(FW_VERSION) + F(" не підтвердилась (") + why + F("), але ") + fail);
    forget();  // nothing to return to: stop counting instead of failing again at every start
    endTrial();
    return;
  }
  String msg = String(FW_VERSION) + F(" не підтвердилась (") + why + F("), повернуто запасну ") + gVer;
  Serial.println(msg);
  otaNote(msg);
  writeFile(CONFIRMED_FILE, gVer);
  endTrial();
  delay(100);
  ESP.restart();
}

void goldenBoot() {
  loadMeta();
  if (!gVer.length() || gVer == FW_VERSION || readFile(CONFIRMED_FILE) == FW_VERSION) {
    endTrial();
    return;
  }
  trial = true;
  String t = readFile(TRIAL_FILE);
  int sp = t.indexOf(' ');
  trialStarts = sp > 0 && t.substring(0, sp) == FW_VERSION ? strtoul(t.substring(sp + 1).c_str(), nullptr, 10) : 0;
  trialStarts++;
  writeFile(TRIAL_FILE, String(FW_VERSION) + ' ' + trialStarts);
  Serial.printf("golden: %s on trial, start %u of %u (golden %s)\n", FW_VERSION, trialStarts, TRIAL_BOOTS - 1,
                gVer.c_str());
  if (trialStarts >= TRIAL_BOOTS) rollback(String(trialStarts - 1) + F(" запуски без зв'язку"));
}

void goldenLoop() {
  bool hbOk = st.lastOkMs != 0;  // a heartbeat got HTTP 200 since this start
  bool cmdOk = !st.cfg.hcRoKey.length() || remoteStatus.startsWith("OK");
  if (trial) {
    if (hbOk && cmdOk) {
      writeFile(CONFIRMED_FILE, FW_VERSION);
      endTrial();
      Serial.printf("golden: %s confirmed\n", FW_VERSION);
    } else if (millis() > TRIAL_MS) {
      rollback(F("15 хв без пінгу й команд"));
    }
    return;
  }
  if (!gVer.length() && !autoSaveTried && hbOk && cmdOk) {  // first version with this feature
    autoSaveTried = true;
    writeFile(CONFIRMED_FILE, FW_VERSION);
    lastSave = goldenSave();
  }
}

String goldenSave() {
  if (trial) return F("ця версія ще не підтвердилась, запасною її не зроблено");
  uint32_t size = ESP.getSketchSize();
  String md5 = ESP.getSketchMD5();
  if (!size || md5.length() != 32) return F("не вдалося прочитати поточну прошивку");
  forget();  // a half-written copy must never be used; this also frees the space of the old one
  FSInfo fs;
  LittleFS.info(fs);
  if (fs.usedBytes + size + FS_SPARE > fs.totalBytes)
    return String(F("замало місця у файловій системі (потрібно ")) + (size / 1024) + F(" КБ)");
  uint8_t *buf = (uint8_t *)malloc(SECTOR);
  if (!buf) return F("замало пам'яті");
  File f = LittleFS.open(IMAGE_FILE, "w");
  bool ok = (bool)f;
  for (uint32_t off = 0; ok && off < size; off += SECTOR) {
    uint32_t n = min(SECTOR, size - off);
    ok = ESP.flashRead(off, (uint32_t *)buf, (n + 3) & ~3u) && f.write(buf, n) == n;
    yield();
  }
  if (f) f.close();
  free(buf);
  if (!ok) {
    forget();
    return F("помилка запису копії");
  }
  if (fileMd5(size) != md5) {
    forget();
    return F("копія не збіглася з оригіналом (MD5)");
  }
  writeFile(META_FILE, String(FW_VERSION) + ' ' + size + ' ' + md5);
  loadMeta();
  Serial.printf("golden: saved %s, %u bytes\n", FW_VERSION, size);
  return String(F("запасна тепер ")) + FW_VERSION + F(" (") + (size / 1024) + F(" КБ)");
}

String goldenVersion() { return gVer; }

bool goldenTrial() { return trial; }

String goldenInfo() {
  String s = gVer.length() ? gVer + F(" (") + (gSize / 1024) + F(" КБ)") : String(F("немає"));
  if (trial) {
    uint32_t left = millis() < TRIAL_MS ? (TRIAL_MS - millis()) / 60000 + 1 : 0;
    s += String(F("; ")) + FW_VERSION + F(" на перевірці: запуск ") + trialStarts + F(" з ") + (TRIAL_BOOTS - 1) +
         F(", до відкату ") + left + F(" хв, якщо не буде пінгу й команд");
  }
  if (lastSave.length()) s += String(F("; ")) + lastSave;
  return s;
}
