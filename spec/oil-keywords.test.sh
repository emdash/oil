# Oil Keywords

#### = to pretty print
= 1 + 2 * 3
## STDOUT:
(Int)   7
## END

#### _ to ignore return value
_ 1 + 2 * 3

var strs = %(a b)
_ len(strs)
_ append(strs, 'c')
write -- @strs

# integer types too
L = [5, 6]
_ append(L, 7)
write -- @L

write __

_ pop(L)  # could also be pop :L
write -- @L

## STDOUT:
a
b
c
5
6
7
__
5
6
## END
