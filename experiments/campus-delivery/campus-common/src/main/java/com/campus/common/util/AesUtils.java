package com.campus.common.util;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;

/**
 * AES-128-GCM 加解密(用于敏感字段如手机号)。
 * 密钥: 环境变量 APP_CRYPTO_KEY(Base64, 16 或 32 字节;32 字节取前 16)。
 * 缺失时回退到开发默认密钥(仅 dev,生产必须外部注入 —— 与 JwtUtils 策略一致)。
 * 密文格式: base64(iv + ciphertext),iv 随机 12 字节。
 */
public final class AesUtils {

    private static final String TRANSFORM = "AES/GCM/NoPadding";
    private static final int IV_LEN = 12;
    private static final int TAG_LEN_BITS = 128;
    private static final SecureRandom RANDOM = new SecureRandom();

    /** 开发默认密钥(16 字节,Base64): 仅当 APP_CRYPTO_KEY 缺失时使用。 */
    private static final String DEV_KEY_B64 = "MTIzNDU2Nzg5MDEyMzQ1Ng=="; // "1234567890123456"

    private static final SecretKeySpec KEY = initKey();

    private AesUtils() {
    }

    private static SecretKeySpec initKey() {
        String b64 = System.getenv("APP_CRYPTO_KEY");
        if (b64 == null || b64.isBlank()) {
            b64 = DEV_KEY_B64;
        }
        byte[] raw = Base64.getDecoder().decode(b64);
        if (raw.length != 16 && raw.length != 32) {
            throw new IllegalArgumentException("APP_CRYPTO_KEY 必须为 16 或 32 字节(Base64 编码)");
        }
        return new SecretKeySpec(raw.length == 32 ? java.util.Arrays.copyOf(raw, 16) : raw, "AES");
    }

    public static String encrypt(String plain) {
        if (plain == null || plain.isEmpty()) {
            return plain;
        }
        try {
            byte[] iv = new byte[IV_LEN];
            RANDOM.nextBytes(iv);
            Cipher cipher = Cipher.getInstance(TRANSFORM);
            cipher.init(Cipher.ENCRYPT_MODE, KEY, new GCMParameterSpec(TAG_LEN_BITS, iv));
            byte[] ct = cipher.doFinal(plain.getBytes(StandardCharsets.UTF_8));
            byte[] out = new byte[iv.length + ct.length];
            System.arraycopy(iv, 0, out, 0, iv.length);
            System.arraycopy(ct, 0, out, iv.length, ct.length);
            return Base64.getEncoder().encodeToString(out);
        } catch (Exception e) {
            throw new IllegalStateException("AES 加密失败", e);
        }
    }

    public static String decrypt(String cipherText) {
        if (cipherText == null || cipherText.isEmpty()) {
            return cipherText;
        }
        try {
            byte[] all = Base64.getDecoder().decode(cipherText);
            if (all.length < IV_LEN + 1) {
                throw new IllegalArgumentException("密文过短");
            }
            byte[] iv = java.util.Arrays.copyOfRange(all, 0, IV_LEN);
            byte[] ct = java.util.Arrays.copyOfRange(all, IV_LEN, all.length);
            Cipher cipher = Cipher.getInstance(TRANSFORM);
            cipher.init(Cipher.DECRYPT_MODE, KEY, new GCMParameterSpec(TAG_LEN_BITS, iv));
            return new String(cipher.doFinal(ct), StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new IllegalStateException("AES 解密失败", e);
        }
    }
}
