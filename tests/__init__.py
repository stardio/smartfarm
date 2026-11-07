"""테스트 패키지: 테스트 로그가 운영 로그(logs/smartfarm.log)에 섞이지 않게 파일 로깅을 끈다."""
import logging

from smartfarm.core import logging as _sf_logging

_sf_logging._configured = True
logging.getLogger().addHandler(logging.NullHandler())
