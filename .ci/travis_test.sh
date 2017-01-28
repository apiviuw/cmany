#!/bin/bash


if [ "$TRAVIS_OS_NAME" == "linux" ] ; then

    PYTHON=python3

elif [ "$TRAVIS_OS_NAME" == "osx" ] ; then

    eval "$(pyenv init -)"
    PYTHON="python$PY"
    pyenv local $PYENV

fi

cwd=$(cd $(dirname $0) ; pwd)
export PYTHONPATH=$cwd/src
$PYTHON -m nose -d -v --with-id --nocapture

exit $?