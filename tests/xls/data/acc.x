proc acc {
    in_ch: chan<u8> in;
    out_ch: chan<u16> out;
    config(in_ch: chan<u8> in, out_ch: chan<u16> out) { (in_ch, out_ch) }
    init { u16:5 }
    next(sum: u16) {
        let (tok, x) = recv(join(), in_ch);
        let s = sum + (x as u16);
        let tok = send_if(tok, out_ch, x == u8:0, s);
        if x == u8:0 { u16:0 } else { s }
    }
}
