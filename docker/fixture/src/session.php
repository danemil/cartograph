<?php
function session_key($user, $nonce) {
    return $user . ':' . $nonce;
}

function is_current($user, $nonce, $key) {
    return session_key($user, $nonce) === $key;
}
