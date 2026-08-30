(() => {
  const VERTEX_SHADER = `
    attribute vec2 a_position;
    varying vec2 v_position;

    void main() {
      v_position = a_position;
      gl_Position = vec4(a_position, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SHADER = `
    precision highp float;

    const float PI = 3.141592653589793;
    uniform sampler2D u_video;
    uniform sampler2D u_subtitle;
    uniform float u_aspect;
    uniform float u_tan_half_fov;
    uniform float u_yaw;
    uniform float u_pitch;
    uniform float u_eye_offset;
    uniform float u_xr_mode;
    uniform float u_subtitle_visible;
    uniform mat4 u_inverse_projection;
    uniform mat4 u_camera_transform;
    varying vec2 v_position;

    vec3 desktop_direction() {
      vec3 view_direction = normalize(vec3(
        v_position.x * u_aspect * u_tan_half_fov,
        v_position.y * u_tan_half_fov,
        -1.0
      ));

      float pitch_cos = cos(u_pitch);
      float pitch_sin = sin(u_pitch);
      vec3 pitched = vec3(
        view_direction.x,
        pitch_cos * view_direction.y - pitch_sin * view_direction.z,
        pitch_sin * view_direction.y + pitch_cos * view_direction.z
      );

      float yaw_cos = cos(u_yaw);
      float yaw_sin = sin(u_yaw);
      return normalize(vec3(
        yaw_cos * pitched.x - yaw_sin * pitched.z,
        pitched.y,
        yaw_sin * pitched.x + yaw_cos * pitched.z
      ));
    }

    vec3 xr_direction() {
      vec4 camera_ray = u_inverse_projection
        * vec4(v_position, 1.0, 1.0);
      camera_ray /= camera_ray.w;
      return normalize(mat3(u_camera_transform) * camera_ray.xyz);
    }

    void main() {
      vec3 direction = u_xr_mode > 0.5
        ? xr_direction()
        : desktop_direction();
      float longitude = atan(direction.x, -direction.z);
      vec4 output_color;
      if (abs(longitude) > PI * 0.5) {
        output_color = vec4(0.0, 0.0, 0.0, 1.0);
      } else {
        float latitude = asin(clamp(direction.y, -1.0, 1.0));
        float eye_u = longitude / PI + 0.5;
        float video_u = u_eye_offset + eye_u * 0.5;
        float video_v = 0.5 - latitude / PI;
        output_color = texture2D(u_video, vec2(video_u, video_v));
      }

      if (u_xr_mode > 0.5 && u_subtitle_visible > 0.5) {
        vec2 subtitle_uv = v_position * 0.5 + 0.5;
        vec4 subtitle_color = texture2D(u_subtitle, subtitle_uv);
        output_color.rgb = mix(
          output_color.rgb,
          subtitle_color.rgb,
          subtitle_color.a
        );
      }
      gl_FragColor = vec4(output_color.rgb, 1.0);
    }
  `;

  const SUBTITLE_SIZE = 1024;
  const clamp = (value, minimum, maximum) =>
    Math.min(maximum, Math.max(minimum, value));

  const compileShader = (gl, type, source) => {
    const shader = gl.createShader(type);
    if (!shader) {
      throw new Error("WebGL 셰이더를 만들 수 없습니다.");
    }
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "알 수 없는 셰이더 오류";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  };

  const createProgram = (gl) => {
    const vertexShader = compileShader(
      gl,
      gl.VERTEX_SHADER,
      VERTEX_SHADER
    );
    const fragmentShader = compileShader(
      gl,
      gl.FRAGMENT_SHADER,
      FRAGMENT_SHADER
    );
    const program = gl.createProgram();
    if (!program) {
      throw new Error("WebGL 프로그램을 만들 수 없습니다.");
    }
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    gl.deleteShader(vertexShader);
    gl.deleteShader(fragmentShader);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "알 수 없는 링크 오류";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  };

  const invertMatrix = (output, matrix) => {
    const a00 = matrix[0];
    const a01 = matrix[1];
    const a02 = matrix[2];
    const a03 = matrix[3];
    const a10 = matrix[4];
    const a11 = matrix[5];
    const a12 = matrix[6];
    const a13 = matrix[7];
    const a20 = matrix[8];
    const a21 = matrix[9];
    const a22 = matrix[10];
    const a23 = matrix[11];
    const a30 = matrix[12];
    const a31 = matrix[13];
    const a32 = matrix[14];
    const a33 = matrix[15];
    const b00 = a00 * a11 - a01 * a10;
    const b01 = a00 * a12 - a02 * a10;
    const b02 = a00 * a13 - a03 * a10;
    const b03 = a01 * a12 - a02 * a11;
    const b04 = a01 * a13 - a03 * a11;
    const b05 = a02 * a13 - a03 * a12;
    const b06 = a20 * a31 - a21 * a30;
    const b07 = a20 * a32 - a22 * a30;
    const b08 = a20 * a33 - a23 * a30;
    const b09 = a21 * a32 - a22 * a31;
    const b10 = a21 * a33 - a23 * a31;
    const b11 = a22 * a33 - a23 * a32;
    const determinant =
      b00 * b11 -
      b01 * b10 +
      b02 * b09 +
      b03 * b08 -
      b04 * b07 +
      b05 * b06;
    if (!determinant) {
      return false;
    }
    const inverseDeterminant = 1.0 / determinant;

    output[0] = (a11 * b11 - a12 * b10 + a13 * b09) * inverseDeterminant;
    output[1] = (a02 * b10 - a01 * b11 - a03 * b09) * inverseDeterminant;
    output[2] = (a31 * b05 - a32 * b04 + a33 * b03) * inverseDeterminant;
    output[3] = (a22 * b04 - a21 * b05 - a23 * b03) * inverseDeterminant;
    output[4] = (a12 * b08 - a10 * b11 - a13 * b07) * inverseDeterminant;
    output[5] = (a00 * b11 - a02 * b08 + a03 * b07) * inverseDeterminant;
    output[6] = (a32 * b02 - a30 * b05 - a33 * b01) * inverseDeterminant;
    output[7] = (a20 * b05 - a22 * b02 + a23 * b01) * inverseDeterminant;
    output[8] = (a10 * b10 - a11 * b08 + a13 * b06) * inverseDeterminant;
    output[9] = (a01 * b08 - a00 * b10 - a03 * b06) * inverseDeterminant;
    output[10] = (a30 * b04 - a31 * b02 + a33 * b00) * inverseDeterminant;
    output[11] = (a21 * b02 - a20 * b04 - a23 * b00) * inverseDeterminant;
    output[12] = (a11 * b07 - a10 * b09 - a12 * b06) * inverseDeterminant;
    output[13] = (a00 * b09 - a01 * b07 + a02 * b06) * inverseDeterminant;
    output[14] = (a31 * b01 - a30 * b03 - a32 * b00) * inverseDeterminant;
    output[15] = (a20 * b03 - a21 * b01 + a22 * b00) * inverseDeterminant;
    return true;
  };

  const wrapText = (context, text, maximumWidth) => {
    const characters = Array.from(text.trim());
    const lines = [];
    let current = "";
    for (const character of characters) {
      const candidate = current + character;
      if (current && context.measureText(candidate).width > maximumWidth) {
        lines.push(current);
        current = character;
      } else {
        current = candidate;
      }
    }
    if (current) {
      lines.push(current);
    }
    return lines;
  };

  const createRenderer = ({
    video,
    canvas,
    onContextLost = () => {},
    onXRChange = () => {},
  }) => {
    const gl = canvas.getContext("webgl", {
      alpha: false,
      antialias: true,
      powerPreference: "high-performance",
      xrCompatible: true,
    });
    if (!gl) {
      throw new Error("이 브라우저에서 WebGL을 사용할 수 없습니다.");
    }

    const program = createProgram(gl);
    const positionLocation = gl.getAttribLocation(program, "a_position");
    const uniforms = {
      aspect: gl.getUniformLocation(program, "u_aspect"),
      cameraTransform: gl.getUniformLocation(program, "u_camera_transform"),
      eyeOffset: gl.getUniformLocation(program, "u_eye_offset"),
      inverseProjection: gl.getUniformLocation(
        program,
        "u_inverse_projection"
      ),
      pitch: gl.getUniformLocation(program, "u_pitch"),
      subtitle: gl.getUniformLocation(program, "u_subtitle"),
      subtitleVisible: gl.getUniformLocation(
        program,
        "u_subtitle_visible"
      ),
      tanHalfFov: gl.getUniformLocation(program, "u_tan_half_fov"),
      video: gl.getUniformLocation(program, "u_video"),
      xrMode: gl.getUniformLocation(program, "u_xr_mode"),
      yaw: gl.getUniformLocation(program, "u_yaw"),
    };
    const positionBuffer = gl.createBuffer();
    const videoTexture = gl.createTexture();
    const subtitleTexture = gl.createTexture();
    if (
      !positionBuffer ||
      !videoTexture ||
      !subtitleTexture ||
      positionLocation < 0
    ) {
      throw new Error("WebGL 렌더링 자원을 만들 수 없습니다.");
    }

    const subtitleCanvas = document.createElement("canvas");
    subtitleCanvas.width = SUBTITLE_SIZE;
    subtitleCanvas.height = SUBTITLE_SIZE;
    const subtitleContext = subtitleCanvas.getContext("2d");
    if (!subtitleContext) {
      throw new Error("VR 자막 캔버스를 만들 수 없습니다.");
    }

    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([
        -1, -1,
        1, -1,
        -1, 1,
        -1, 1,
        1, -1,
        1, 1,
      ]),
      gl.STATIC_DRAW
    );
    gl.enableVertexAttribArray(positionLocation);
    gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, videoTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      1,
      1,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      new Uint8Array([0, 0, 0, 255])
    );
    gl.uniform1i(uniforms.video, 0);

    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, subtitleTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      SUBTITLE_SIZE,
      SUBTITLE_SIZE,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      null
    );
    gl.uniform1i(uniforms.subtitle, 1);

    let active = false;
    let animationFrame = null;
    let dirty = true;
    let fieldOfView = 75;
    let pitch = 0;
    let yaw = 0;
    let pointerId = null;
    let pointerX = 0;
    let pointerY = 0;
    let subtitleDirty = true;
    let subtitleLines = [];
    let xrSession = null;
    let xrReferenceSpace = null;
    let xrLayer = null;
    const inverseProjection = new Float32Array(16);
    const identityMatrix = new Float32Array([
      1, 0, 0, 0,
      0, 1, 0, 0,
      0, 0, 1, 0,
      0, 0, 0, 1,
    ]);

    const prepareProgram = () => {
      gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.enableVertexAttribArray(positionLocation);
      gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, videoTexture);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, subtitleTexture);
    };

    const updateVideoTexture = () => {
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        return;
      }
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, videoTexture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        video
      );
    };

    const redrawSubtitleCanvas = () => {
      subtitleContext.clearRect(0, 0, SUBTITLE_SIZE, SUBTITLE_SIZE);
      if (!subtitleLines.length) {
        return;
      }
      const fontSize = subtitleLines.length > 2 ? 42 : 50;
      const lineHeight = Math.round(fontSize * 1.35);
      subtitleContext.font =
        `650 ${fontSize}px "Noto Sans KR", "Noto Sans CJK KR", ` +
        '"Apple SD Gothic Neo", "Malgun Gothic", sans-serif';
      subtitleContext.textAlign = "center";
      subtitleContext.textBaseline = "middle";
      const rendered = subtitleLines
        .flatMap((line) =>
          wrapText(subtitleContext, line.text, SUBTITLE_SIZE - 120).map(
            (text) => ({ text, color: line.color })
          )
        )
        .slice(0, 5);
      const firstY =
        SUBTITLE_SIZE - 82 - lineHeight * Math.max(0, rendered.length - 1);
      rendered.forEach((line, index) => {
        const y = firstY + index * lineHeight;
        const width = subtitleContext.measureText(line.text).width;
        subtitleContext.fillStyle = "rgba(0, 0, 0, 0.78)";
        subtitleContext.fillRect(
          SUBTITLE_SIZE / 2 - width / 2 - 18,
          y - lineHeight * 0.48,
          width + 36,
          lineHeight * 0.96
        );
        subtitleContext.fillStyle = line.color;
        subtitleContext.shadowColor = "#000";
        subtitleContext.shadowBlur = 6;
        subtitleContext.fillText(line.text, SUBTITLE_SIZE / 2, y);
      });
      subtitleContext.shadowBlur = 0;
    };

    const updateSubtitleTexture = () => {
      if (!subtitleDirty) {
        return;
      }
      subtitleDirty = false;
      redrawSubtitleCanvas();
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, subtitleTexture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        subtitleCanvas
      );
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    };

    const drawView = ({
      aspect,
      eyeOffset,
      xrMode,
      projection = identityMatrix,
      cameraTransform = identityMatrix,
    }) => {
      prepareProgram();
      gl.uniform1f(uniforms.aspect, aspect);
      gl.uniform1f(uniforms.eyeOffset, eyeOffset);
      gl.uniform1f(uniforms.pitch, pitch);
      gl.uniform1f(
        uniforms.subtitleVisible,
        xrMode && subtitleLines.length ? 1 : 0
      );
      gl.uniform1f(
        uniforms.tanHalfFov,
        Math.tan((fieldOfView * Math.PI) / 360)
      );
      gl.uniform1f(uniforms.xrMode, xrMode ? 1 : 0);
      gl.uniform1f(uniforms.yaw, yaw);
      gl.uniformMatrix4fv(uniforms.inverseProjection, false, projection);
      gl.uniformMatrix4fv(
        uniforms.cameraTransform,
        false,
        cameraTransform
      );
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    };

    const resize = () => {
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(canvas.clientWidth * pixelRatio));
      const height = Math.max(
        1,
        Math.round(canvas.clientHeight * pixelRatio)
      );
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      return width / height;
    };

    const drawDesktop = () => {
      if (!active || xrSession || gl.isContextLost()) {
        return;
      }
      const aspect = resize();
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      updateVideoTexture();
      drawView({
        aspect,
        eyeOffset: 0,
        xrMode: false,
      });
    };

    const desktopFrame = () => {
      animationFrame = null;
      if (!active || xrSession) {
        return;
      }
      if (dirty || (!video.paused && !video.ended)) {
        dirty = false;
        drawDesktop();
      }
      if (!video.paused && !video.ended) {
        animationFrame = window.requestAnimationFrame(desktopFrame);
      }
    };

    const requestDraw = () => {
      dirty = true;
      if (active && !xrSession && animationFrame === null) {
        animationFrame = window.requestAnimationFrame(desktopFrame);
      }
    };

    const reset = () => {
      fieldOfView = 75;
      pitch = 0;
      yaw = 0;
      requestDraw();
    };

    const finishXRSession = () => {
      xrSession = null;
      xrReferenceSpace = null;
      xrLayer = null;
      onXRChange(false);
      requestDraw();
    };

    const drawXRFrame = (_time, frame) => {
      const session = frame.session;
      if (session !== xrSession || !xrLayer || !xrReferenceSpace) {
        return;
      }
      session.requestAnimationFrame(drawXRFrame);
      const pose = frame.getViewerPose(xrReferenceSpace);
      if (!pose) {
        return;
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, xrLayer.framebuffer);
      gl.clearColor(0, 0, 0, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      updateVideoTexture();
      updateSubtitleTexture();
      for (const view of pose.views) {
        const viewport = xrLayer.getViewport(view);
        if (!viewport || !invertMatrix(inverseProjection, view.projectionMatrix)) {
          continue;
        }
        gl.viewport(
          viewport.x,
          viewport.y,
          viewport.width,
          viewport.height
        );
        drawView({
          aspect: viewport.width / Math.max(1, viewport.height),
          eyeOffset: view.eye === "right" ? 0.5 : 0,
          xrMode: true,
          projection: inverseProjection,
          cameraTransform: view.transform.matrix,
        });
      }
    };

    const enterXR = async () => {
      if (xrSession) {
        return;
      }
      if (!window.isSecureContext) {
        throw new Error("실제 VR 헤드셋 모드는 HTTPS에서만 사용할 수 있습니다.");
      }
      if (!navigator.xr) {
        throw new Error("이 브라우저는 WebXR immersive-vr을 지원하지 않습니다.");
      }
      const session = await navigator.xr.requestSession("immersive-vr", {
        optionalFeatures: ["local-floor"],
      });
      try {
        if (typeof gl.makeXRCompatible === "function") {
          await gl.makeXRCompatible();
        }
        if (typeof window.XRWebGLLayer !== "function") {
          throw new Error("XRWebGLLayer를 사용할 수 없습니다.");
        }
        const layer = new window.XRWebGLLayer(session, gl, {
          alpha: false,
          antialias: true,
          depth: false,
        });
        session.updateRenderState({
          baseLayer: layer,
          depthNear: 0.1,
          depthFar: 10,
        });
        let referenceSpace;
        try {
          referenceSpace = await session.requestReferenceSpace("local-floor");
        } catch (_error) {
          referenceSpace = await session.requestReferenceSpace("local");
        }
        xrSession = session;
        xrLayer = layer;
        xrReferenceSpace = referenceSpace;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
        session.addEventListener("end", finishXRSession, { once: true });
        session.addEventListener("select", () => {
          if (video.paused) {
            video.play().catch(() => {});
          } else {
            video.pause();
          }
        });
        onXRChange(true);
        session.requestAnimationFrame(drawXRFrame);
        if (video.paused) {
          video.play().catch(() => {});
        }
      } catch (error) {
        await session.end().catch(() => {});
        throw error;
      }
    };

    const exitXR = async () => {
      if (xrSession) {
        await xrSession.end();
      }
    };

    const onPointerDown = (event) => {
      pointerId = event.pointerId;
      pointerX = event.clientX;
      pointerY = event.clientY;
      canvas.setPointerCapture?.(event.pointerId);
      canvas.classList.add("is-dragging");
      canvas.focus({ preventScroll: true });
    };

    const onPointerMove = (event) => {
      if (event.pointerId !== pointerId) {
        return;
      }
      const deltaX = event.clientX - pointerX;
      const deltaY = event.clientY - pointerY;
      pointerX = event.clientX;
      pointerY = event.clientY;
      yaw = clamp(yaw - deltaX * 0.005, -Math.PI / 2, Math.PI / 2);
      pitch = clamp(
        pitch + deltaY * 0.005,
        -Math.PI * 0.47,
        Math.PI * 0.47
      );
      requestDraw();
    };

    const onPointerUp = (event) => {
      if (event.pointerId !== pointerId) {
        return;
      }
      canvas.releasePointerCapture?.(event.pointerId);
      pointerId = null;
      canvas.classList.remove("is-dragging");
    };

    const onWheel = (event) => {
      event.preventDefault();
      fieldOfView = clamp(fieldOfView + event.deltaY * 0.04, 35, 105);
      requestDraw();
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener(
      "webglcontextlost",
      (event) => {
        event.preventDefault();
        active = false;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
        if (xrSession) {
          xrSession.end().catch(() => {});
        }
        onContextLost();
      },
      { once: true }
    );
    const redrawEvents = [
      "loadeddata",
      "play",
      "seeked",
      "timeupdate",
    ];
    for (const eventName of redrawEvents) {
      video.addEventListener(eventName, requestDraw);
    }
    window.addEventListener("resize", requestDraw);

    return {
      enterXR,
      exitXR,
      destroy() {
        active = false;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
        if (xrSession) {
          xrSession.end().catch(() => {});
        }
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointercancel", onPointerUp);
        canvas.removeEventListener("wheel", onWheel);
        for (const eventName of redrawEvents) {
          video.removeEventListener(eventName, requestDraw);
        }
        window.removeEventListener("resize", requestDraw);
      },
      get isXRPresenting() {
        return xrSession !== null;
      },
      requestDraw,
      reset,
      setSubtitleLines(lines) {
        subtitleLines = Array.from(lines || [], (line) => ({
          color: /^#[0-9a-f]{6}$/i.test(String(line.color))
            ? String(line.color)
            : "#ffffff",
          text: String(line.text || "").trim(),
        })).filter((line) => line.text);
        subtitleDirty = true;
      },
      start() {
        active = true;
        requestDraw();
      },
      stop() {
        active = false;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
        if (xrSession) {
          xrSession.end().catch(() => {});
        }
      },
    };
  };

  const isImmersiveVRSupported = async () => {
    if (!window.isSecureContext || !navigator.xr) {
      return false;
    }
    try {
      return await navigator.xr.isSessionSupported("immersive-vr");
    } catch (_error) {
      return false;
    }
  };

  window.createVR180Renderer = createRenderer;
  window.isImmersiveVRSupported = isImmersiveVRSupported;
})();
