// A module imported by its upstream path (up.util.inc).
pub fn inc(x: u8) -> u8 { x + u8:1 }

#[test]
fn inc_test() { assert_eq(u8:2, inc(u8:1)) }
