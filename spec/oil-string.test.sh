#### single quoted -- implicit and explicit raw
var x = 'foo bar'
echo $x
setvar x = r'foo bar'  # Same string
echo $x
setvar x = r'\t\n'  # This is raw
echo $x
## STDOUT:
foo bar
foo bar
\t\n
## END

#### Implicit raw single quote with backslash is a syntax error
var x = '\t\n'
echo $x
## status: 2
## stdout-json: ""

#### single quoted C strings: $'foo\n'

# expression mode
var x = $'foo\nbar'
echo "$x"

# command mode
if test "$x" = $'foo\nbar'; then
  echo equal
fi

## STDOUT:
foo
bar
equal
## END

#### Double Quoted
var name = 'World'
var g = "Hello $name"

echo "Hello $name"
echo $g
## STDOUT:
Hello World
Hello World
## END

#### Multiline strings with '' and ""

var single = '
  single
'

var x = 42
var double = "
  double $x
"

echo $single
echo $double

## STDOUT:

  single


  double 42

## END

#### C strings in %() array literals
shopt -s oil:basic

var lines=%($'aa\tbb' $'cc\tdd')
write @lines

## STDOUT:
aa	bb
cc	dd
## END

#### shopt parse_raw_string

# Ignored prefix
echo r'\'

# These use shell rules!
echo ra'\'
echo raw'\'

echo r"\\"

# Now it's a regular r
shopt --unset parse_raw_string
write unset r'\'

## STDOUT:
\
ra\
raw\
r\
unset
r\
## END

#### Triple Double Quotes, Expression Mode

var two = 2
var three = 2
var x = """
  one
  two = $two
  three = $three
  """
echo $x

## STDOUT:
one
two = 2
three = 3
## END

#### Triple Single Quotes, Expression Mode

var two = 2
var three = 2
var x = '''
  one
  two = $two
  three = $three
  \u{61}
  '''
echo $x

var x = $'''
  one
  two = $two
  three = $three
  \u{61}
  '''
echo $x

## STDOUT:
one
two = $two
three = $three
\u{61}
## END


#### Triple Double Quotes, Command Mode

var two=2
var three=3

echo """
  one
  two = $two
  three = $three
  """

shopt --set parse_triple_quoted

# dedent, and first newline doesn't count
echo """
  one
  two = $two
  three = $three
  """

## STDOUT:

  one
  two = 2
  three = 3
  
one
two = 2
three = 3
## END

#### Triple Single Quotes, Command Mode

echo '''
  one
  two = $two
  three = $three
  \u{61}
  '''

shopt --set parse_triple_quoted

# dedent, and first newline doesn't count
echo '''
  one
  two = $two
  three = $three
  \u{61}
  '''

echo $'''
  one
  two = $two
  three = $three
  \u{61}
  '''

## STDOUT:

  one
  two = $two
  three = $three

one
two = $two
three = $three
## END

#### here doc with quotes

# This has 3 right double quotes

cat <<EOF
"hello"
""
"""
EOF


## STDOUT:
"hello"
""
"""
## END
