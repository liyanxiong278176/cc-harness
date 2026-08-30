package com.campus.common.util;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class AesUtilsTest {

    @Test
    void encryptThenDecryptRoundTrip() {
        String plain = "13800138000";
        String cipher = AesUtils.encrypt(plain);
        assertNotEquals(plain, cipher);
        assertEquals(plain, AesUtils.decrypt(cipher));
    }

    @Test
    void randomIvProduceDifferentCiphertexts() {
        String cipher1 = AesUtils.encrypt("hello");
        String cipher2 = AesUtils.encrypt("hello");
        assertNotEquals(cipher1, cipher2);
        assertEquals("hello", AesUtils.decrypt(cipher1));
        assertEquals("hello", AesUtils.decrypt(cipher2));
    }

    @Test
    void nullOrEmptyPassThrough() {
        assertEquals(null, AesUtils.encrypt(null));
        assertEquals("", AesUtils.decrypt(""));
    }

    @Test
    void invalidCiphertextThrows() {
        assertThrows(IllegalStateException.class, () -> AesUtils.decrypt("not-base64!!"));
        assertThrows(IllegalStateException.class, () -> AesUtils.decrypt("E_")); // 过短密文
    }
}
