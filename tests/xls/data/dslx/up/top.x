import up.util.inc;

fn twice(x: u8) -> u8 { inc::inc(inc::inc(x)) }

#[test]
fn twice_test() { assert_eq(u8:3, twice(u8:1)) }
