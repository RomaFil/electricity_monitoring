#include "trust.h"

#include "certs.h"

static BearSSL::X509List *sectigo = nullptr;
static BearSSL::X509List *letsEncrypt = nullptr;

void trustBegin() {
  if (!sectigo) sectigo = new BearSSL::X509List(ROOT_CA_SECTIGO_E46);
  if (!letsEncrypt) letsEncrypt = new BearSSL::X509List(ROOT_CA_LETSENCRYPT);
}

BearSSL::X509List *trustSectigo() {
  trustBegin();
  return sectigo;
}

BearSSL::X509List *trustLetsEncrypt() {
  trustBegin();
  return letsEncrypt;
}
