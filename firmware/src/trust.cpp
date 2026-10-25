#include "trust.h"

#include "certs.h"

static BearSSL::X509List *sectigo = nullptr;
void trustBegin() {
  if (!sectigo) sectigo = new BearSSL::X509List(ROOT_CA_SECTIGO_E46);
}

BearSSL::X509List *trustSectigo() {
  trustBegin();
  return sectigo;
}
