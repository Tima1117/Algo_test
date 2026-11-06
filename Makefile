# Весь путь от сбора до готового сайта: make all
# Зависимости ставятся в свой .venv — на свежей Ubuntu системный pip закрыт (PEP 668).
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: all install collect build test site clean-raw

all: install collect site            ## сбор → данные → сайт

$(PY):
	python3 -m venv $(VENV)

install: $(PY)                       ## python-зависимости в .venv и npm-пакеты сайта
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e '.[dev]'
	cd site && npm ci

collect:                            ## реестр + сбор + нормализация + притоки + отчёт качества
	$(PY) -m pif all --strict

build:                              ## только пересборка данных из уже собранного (без сети)
	$(PY) -m pif build

test:
	$(PY) -m pytest -q

site:                               ## статический сайт в site/dist/pif/
	cd site && npm run build

clean-raw:                          ## удалить кэш сырых ответов (их восстановит следующий сбор)
	rm -rf raw
