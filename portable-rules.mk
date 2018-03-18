# portable-rules.mk: These are done on the dev machine.
# 
# Non-portable rules involves C compilers, and must be done on the target
# machine.

# The root of this repo, e.g. ~/git/oil, should be our PYTHONPATH for
# detecting dependencies.
# 
# From this link:
# https://stackoverflow.com/questions/322936/common-gnu-makefile-directory-path
# Except we're using 'firstword' instead of 'lastword', because
# _build/oil/ovm.d is the last one.
REPO_ROOT := $(abspath $(dir $(firstword $(MAKEFILE_LIST))))

#
# App-independent rules.
#

# NOTES:
# - Manually rm this file to generate a new build timestamp.
# - This messes up reproducible builds.
# - It's not marked .PHONY because that would mess up the end user build.
#   bytecode-*.zip should NOT be built by the user.
_build/release-date.txt:
	$(ACTIONS_SH) write-release-date

# The Makesfiles generated by autoconf don't call configure, but Linux/toybox
# config system does.  This can be overridden.
_build/detected-config.sh:
	./configure

# What files correspond to each C module.
# TODO:
# - Where to put -l z?  (Done in Modules/Setup.dist)
_build/c-module-toc.txt: build/c_module_toc.py
	$(ACTIONS_SH) c-module-toc > $@

# Python and C dependencies of runpy.
# NOTE: This is done with a pattern rule because of the "multiple outputs"
# problem in Make.
_build/runpy-deps-%.txt: build/runpy_deps.py
	$(ACTIONS_SH) runpy-deps _build

_build/py-to-compile.txt: build/runpy_deps.py
	$(ACTIONS_SH) runpy-py-to-compile > $@

#
# App-Independent Pattern Rules.
#

# Regenerate dependencies.  But only if we made the app dirs.
_build/%/ovm.d: _build/%/app-deps-c.txt
	$(ACTIONS_SH) make-dotd $* $^ > $@

# Source paths of all C modules the app depends on.  For the tarball.
# A trick: remove the first dep to form the lists.  You can't just use $^
# because './c_module_srcs.py' is rewritten to 'c_module_srcs.py'.
_build/%/c-module-srcs.txt: \
	build/c_module_srcs.py _build/c-module-toc.txt _build/%/app-deps-c.txt
	build/c_module_srcs.py $(filter-out $<,$^) > $@

_build/%/all-deps-c.txt: build/static-c-modules.txt _build/%/app-deps-c.txt
	$(ACTIONS_SH) join-modules $^ > $@

# NOTE: This should really depend on all the .py files.
# I should make a _build/oil/py.d file and include it?
# This depends on the grammar pickle because it's the first one that calls opy compile.
_build/%/opy-app-deps.txt: \
	_build/opy/py27.grammar.pickle _build/py-to-compile.txt _build/%/py-to-compile.txt 
	# exclude the pickle
	sort $(filter-out $<,$^) | uniq | opy/build.sh compile-manifest _build/$*/bytecode-opy > $@


PY27 := Python-2.7.13

# Per-app extension module initialization.
_build/%/module_init.c: $(PY27)/Modules/config.c.in _build/%/all-deps-c.txt
	# NOTE: Using xargs < input.txt style because it will fail if input.txt
	# doesn't exist!  'cat' errors will be swallowed.
	xargs $(ACTIONS_SH) gen-module-init < _build/$*/all-deps-c.txt > $@


# 
# Tarballs
#
# Contain Makefile and associated shell scripts, discovered .c and .py deps,
# app source.

_release/%.tar: _build/%/$(BYTECODE_ZIP) \
                _build/%/module_init.c \
                _build/%/main_name.c \
                _build/%/c-module-srcs.txt
	$(COMPILE_SH) make-tar $* $(BYTECODE_ZIP) $@

