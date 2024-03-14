# $FreeBSD: stable/9/share/skel/dot.shrc 222932 2011-06-10 13:47:11Z jilles $
#
# .shrc - bourne shell startup file 
#
# This file will be used if the shell is invoked for interactive use and
# the environment variable ENV is set to this file.
#
# see also sh(1), environ(7).
#


# file permissions: rwxr-xr-x
#
# umask	022

# Enable the builtin emacs(1) command line editor in sh(1),
# e.g. C-a -> beginning-of-line.
set -o emacs

# Uncomment this and comment the above to enable the builtin vi(1) command
# line editor in sh(1), e.g. ESC to go into visual mode.
# set -o vi


# some useful aliases
alias h='fc -l'
alias j=jobs
alias m=$PAGER
alias ll='ls -laFo'
alias l='ls -l'
alias g='egrep -i'
 
# # be paranoid
# alias cp='cp -ip'
# alias mv='mv -i'
# alias rm='rm -i'


# # set prompt: ``username@hostname$ '' 
# PS1="`whoami`@`hostname | sed 's/\..*//'`"
# case `id -u` in
# 	0) PS1="${PS1}# ";;
# 	*) PS1="${PS1}$ ";;
# esac

# search path for cd(1)
# CDPATH=:$HOME

# Decrease likelihood of filesystem metadata corruption on [CF,SD,USB]
# persistent media by setting '-o noatime'.
alias mountrw='mount -o noatime -uw'
