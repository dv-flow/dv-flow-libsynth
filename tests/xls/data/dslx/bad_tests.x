fn f(x: u8) -> u8 { x + u8:1 }

#[test]
fn good() { assert_eq(u8:2, f(u8:1)) }

#[test]
fn bad() {
    let y = f(u8:3);
    assert_eq(u8:5, y)
}
